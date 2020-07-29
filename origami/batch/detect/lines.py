#!/usr/bin/env python3

import click
import json
import numpy as np
import cv2
import logging

from pathlib import Path

from origami.batch.core.processor import Processor
from origami.batch.core.io import Artifact, Stage, Input, Output
from origami.core.block import ConcurrentLineDetector, TextAreaFactory
from origami.batch.core.utils import RegionsFilter
from origami.batch.core.lines import reliable_contours


def scale_grid(s0, s1, grid):
	h0, w0 = s0
	h1, w1 = s1
	grid[:, :, 0] *= w1 / w0
	grid[:, :, 1] *= h1 / h0


class ConfidenceSampler:
	def __init__(self, blocks, segmentation):
		self._predictions = dict()
		for p in segmentation.predictions:
			self._predictions[p.name] = p

		self._page = list(blocks.values())[0].page
		self._page_shape = tuple(reversed(self._page.warped.size))

	def __call__(self, path, line, res=0.5):
		prediction_name, predictor_class = path[:2]

		predictor = self._predictions[prediction_name]

		grid = line.warped_grid(xres=res, yres=res)

		scale_grid(self._page_shape, predictor.labels.shape, grid)
		labels = cv2.remap(predictor.labels, grid, None, cv2.INTER_NEAREST)

		counts = np.bincount(labels.flatten(), minlength=len(predictor.classes))

		evidence = dict()

		sum_all = np.sum(counts)
		if sum_all > 0:
			for k in predictor.classes:
				key = "%s/%s" % (prediction_name, k.name)
				evidence[key] = counts[k.value] / sum_all

		return evidence


class LineDetectionProcessor(Processor):
	def __init__(self, options):
		super().__init__(options)
		self._options = options
		self._allow_conflicts = RegionsFilter(options["allow_conflicts"])
		self._min_confidence = 0

	@property
	def processor_name(self):
		return __loader__.name

	def artifacts(self):
		return [
			("warped", Input(Artifact.SEGMENTATION, stage=Stage.WARPED)),
			("aggregate", Input(Artifact.CONTOURS, stage=Stage.AGGREGATE)),
			("output", Output(Artifact.CONTOURS, Artifact.LINES, stage=Stage.RELIABLE))
		]

	def process(self, page_path: Path, warped, aggregate, output):
		blocks = aggregate.regions.by_path
		if not blocks:
			return

		sampler = ConfidenceSampler(blocks, warped.segmentation)

		conflicting_blocks = [
			block for path, block in blocks.items()
			if not self._allow_conflicts(path)]

		detector = ConcurrentLineDetector(
			text_area_factory=TextAreaFactory(
				conflicting_blocks,
				buffer=self._options["contours_buffer"]),
			force_parallel_lines=False,
			force_lines=True,
			extra_height=self._options["extra_height"],
			extra_descent=self._options["extra_descent"])

		detected_lines_by_block = detector(blocks)

		for block_path, lines in detected_lines_by_block.items():
			for line in lines:
				line.update_confidence(sampler(block_path, line))

		detected_lines = dict()
		free_lines = []
		for parts, lines in detected_lines_by_block.items():
			prediction_name = parts[0]
			class_name = parts[1]
			block_id = parts[2]

			for line_id, line in enumerate(lines):
				pred_path = line.predicted_path
				if pred_path != (prediction_name, class_name):
					free_lines.append((pred_path, line))
				else:
					line_path = (prediction_name, class_name, block_id, line_id)
					detected_lines[line_path] = line

		reliable = reliable_contours(
			blocks, free_lines, detected_lines)

		with output.lines() as zf:
			info = dict(version=1, min_confidence=self._min_confidence)
			zf.writestr("meta.json", json.dumps(info))

			for line_path, line in detected_lines.items():
				zf.writestr("%s.json" % "/".join(map(str, line_path)), json.dumps(line.info))

		with output.contours(copy_meta_from=aggregate) as zf:
			for k, contour in reliable.items():
				if contour.geom_type != "Polygon" and not contour.is_empty:
					logging.error(
						"reliable contour %s is %s" % (k, contour.geom_type))
				zf.writestr("/".join(map(str, k)) + ".wkt", contour.wkt)


@click.command()
@click.option(
	'--extra-height',
	default=0.075,
	type=float,
	help='compensate underestimated line height')
@click.option(
	'--extra-descent',
	default=0.025,
	type=float,
	help='compensate underestimated line descent')
@click.option(
	'--contours-buffer',
	default=0.001,
	type=float,
	help='expand contours by specified relative amount')
@click.option(
	'--allow-conflicts',
	default="regions/ILLUSTRATION",
	type=str,
	help='regions types that may overlap without being resolved')
@click.argument(
	'data_path',
	type=click.Path(exists=True),
	required=True)
@Processor.options
def detect_lines(data_path, **kwargs):
	""" Perform line detection on all document images in DATA_PATH. Needs
	information from contours batch. """
	processor = LineDetectionProcessor(kwargs)
	processor.traverse(data_path)


if __name__ == "__main__":
	detect_lines()

