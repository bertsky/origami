#!/usr/bin/env python3

import click
import collections
import codecs
import logging
import io
import shapely

from pathlib import Path
from tabulate import tabulate
from cached_property import cached_property

from origami.batch.core.processor import Processor
from origami.batch.core.io import Artifact, Stage, Input, Output
from origami.batch.core.utils import RegionsFilter, TableRegionCombinator
import origami.pagexml.pagexml as pagexml


def sorted_by_keys(x):
	return [x[k] for k in sorted(list(x.keys()))]


def polygon_union(geoms):
	shape = shapely.ops.cascaded_union(geoms)
	if shape.geom_type != "Polygon":
		shape = shape.convex_hull
	return shape


class MergedTextRegion:
	def __init__(self, document, block_path, lines):
		self._block_path = block_path
		self._polygon = polygon_union([
			line.image_space_polygon for _, line in lines])
		self._document = document
		self._transform = document.rewarp
		self._lines = lines

	def export_page_xml(self, px_document):
		px_region = px_document.append_region(
			"TextRegion", id_="-".join(self._block_path))
		px_region.append_coords(self._transform(
			self._polygon.exterior.coords))

		for i, (line_path, line) in enumerate(self._lines):
			line_text = self._document.get(line_path[:3]).get_line_text(line_path)
			px_line = px_region.append_text_line(id_="-".join(self._block_path + (str(i),)))
			px_line.append_coords(self._transform(
				line.image_space_polygon.exterior.coords))
			px_line.append_text_equiv(line_text)


class TextRegion:
	def __init__(self, document, block_path):
		blocks, lines = document.blocks_and_lines(block_path)

		assert len(blocks) == 1
		_, block = blocks[0]
		self._polygon = block.image_space_polygon

		self._block_path = block_path

		self._lines = lines
		self._line_texts = dict()

		self._order = []
		self._transform = document.rewarp

	@property
	def polygon(self):
		return self._polygon

	def get_line_text(self, line_path):
		return self._line_texts[line_path]

	def export_plain_text_region(self, composition):
		for p in self._order:
			composition.append_text(p, self._line_texts[p])

	def export_plain_text_line(self, composition, line_path):
		composition.append_text(
			line_path, self._line_texts[line_path])

	def export_page_xml(self, px_document):
		px_region = px_document.append_region(
			"TextRegion", id_="-".join(self._block_path))

		px_region.append_coords(self._transform(
			self._polygon.exterior.coords))

		for line_path in self._order:
			line = self._lines[line_path]

			if line.image_space_polygon.is_empty:
				if self._line_texts[line_path]:
					raise RuntimeError(
						"line %s has text '%s', confidence %.2f, but empty geometry" % (
							str(line_path), self._line_texts[line_path], line.confidence))
				continue

			px_line = px_region.append_text_line(id_="-".join(line_path))
			px_line.append_coords(self._transform(
				line.image_space_polygon.exterior.coords))
			px_line.append_text_equiv(self._line_texts[line_path])

	def add_text(self, line_path, text):
		self._order.append(line_path)
		self._line_texts[line_path] = text


class TableRegion:
	def __init__(self, document, block_path):
		blocks, lines = document.blocks_and_lines(block_path)

		self._lines = lines
		self._block_path = block_path
		self._divisions = set()
		self._rows = collections.defaultdict(set)
		self._columns = set()
		self._texts = collections.defaultdict(list)
		self._transform = document.rewarp
		self._document = document

		self._blocks = dict()
		for path, block in blocks:
			block_id, division, row, column = map(int, path[2].split("."))
			self._blocks[(column, division, row)] = block

	def export_plain_text_region(self, composition):
		composition.append_text(
			self._block_path, self.to_text())

	def export_page_xml(self, px_document):
		table_id = "-".join(self._block_path)
		px_table_region = px_document.append_region(
			"TableRegion", id_=table_id)

		columns = sorted(list(self._columns))
		divisions = sorted(list(self._divisions))
		column_shapes = []

		for column in columns:
			column_id = "%s.%d" % (table_id, column)
			px_column = px_table_region.append_text_region(id_=column_id)
			division_shapes = []

			for division in divisions:
				division_id = "%s.%d" % (column_id, division)
				px_division = px_column.append_text_region(id_=division_id)
				cell_shapes = []

				rows = sorted(list(self._rows[division]))
				for row in rows:
					block = self._blocks.get((column, division, row))
					if not block:
						continue
					if block.image_space_polygon.is_empty:
						continue

					cell_id = "%s.%d" % (division_id, row)
					px_cell = px_division.append_text_region(id_=cell_id)
					px_cell.append_coords(self._transform(
						block.image_space_polygon.exterior.coords))

					texts = self._texts.get((division, row, column), [])
					for line_path, text in texts:
						px_line = px_cell.append_text_line(
							id_="-".join(line_path))
						px_line.append_coords(self._transform(
							self._lines[line_path].image_space_polygon.exterior.coords))
						px_line.append_text_equiv(text)

					cell_shapes.append(block.image_space_polygon)

				if cell_shapes:
					division_shape = polygon_union(cell_shapes)
					px_division.prepend_coords(self._transform(
						division_shape.exterior.coords))
					division_shapes.append(division_shape)
				else:
					px_column.remove(px_division)

			if division_shapes:
				column_shape = polygon_union(division_shapes)
				px_column.prepend_coords(self._transform(
					column_shape.exterior.coords))
				column_shapes.append(column_shape)
			else:
				px_table_region.remove(px_column)

		if column_shapes:
			shape = polygon_union(column_shapes)
			px_table_region.prepend_coords(self._transform(
				shape.exterior.coords))
		else:
			logging.warning("table %s was empty on page %s." % (
				str(self._block_path), self._document.page_path))
			px_document.remove(px_table_region)

	def append_cell_text(self, grid, line_path, text):
		division, row, column = tuple(map(int, grid))
		self._divisions.add(division)
		self._rows[division].add(row)
		self._columns.add(column)
		self._texts[(division, row, column)].append((line_path, text))

	def to_text(self):
		columns = sorted(list(self._columns))
		table_data = []
		n_rows = []

		divisions = sorted(list(self._divisions))
		for division in divisions:
			rows = sorted(list(self._rows[division]))
			n_rows.append(len(rows))
			for row in rows:
				row_data = []
				for column in columns:
					texts = [s.strip() for _, s in self._texts.get(
						(division, row, column), [])]
					row_data.append("\n".join(texts))
				table_data.append(row_data)

		if len(columns) == 1:
			return "\n".join(["".join(x) for x in table_data])
		else:
			if len(n_rows) >= 2 and n_rows[0] == 1:
				headers = "firstrow"
			else:
				headers = ()

			return tabulate(
				table_data, tablefmt="psql", headers=headers)


class GraphicRegion:
	def __init__(self, document, block_path):
		blocks, lines = document.blocks_and_lines(block_path)
		assert len(blocks) == 1
		self._block = blocks[0][1]
		self._lines = lines
		self._block_path = block_path
		self._transform = document.rewarp

	def export_page_xml(self, px_document):
		px_region = px_document.append_region(
			"GraphicRegion", id_="-".join(self._block_path))
		px_region.append_coords(self._transform(
			self._block.image_space_polygon.exterior.coords))


class Document:
	def __init__(self, page_path, input):
		self._page_path = page_path
		self._input = input
		self._grid = self.page.dewarper.grid

		combinator = TableRegionCombinator(input.regions.by_path.keys())
		self._mapping = combinator.mapping

		region_lines = collections.defaultdict(list)
		for line_path, line in input.lines.by_path.items():
			region_lines[line_path[:3]].append((line_path, line))
		self._region_lines = region_lines

		self._regions = dict()

		# add lines and line texts in correct order.
		for line_path, ocr_text in input.sorted_ocr:
			block_path = line_path[:3]

			table_path = block_path[2].split(".")
			if len(table_path) > 1:
				assert block_path[:2] == ("regions", "TABULAR")
				base_block_path = block_path[:2] + (table_path[0],)

				self._add(TableRegion, base_block_path).append_cell_text(
					table_path[1:], line_path, ocr_text)
			else:
				assert block_path[:2] == ("regions", "TEXT")
				self._add(TextRegion, block_path).add_text(
					line_path, ocr_text)

		# add graphics regions.
		for block_path, block in input.regions.by_path.items():
			if block_path[:2] == ("regions", "ILLUSTRATION"):
				self._add(GraphicRegion, block_path)

	@property
	def page_path(self):
		return self._page_path

	@property
	def reading_order(self):
		order_data = self._input.order
		return list(map(
			lambda x: tuple(x.split("/")), order_data["orders"]["*"]))

	def rewarp(self, coords):
		warped_coords = self._grid.inverse(coords)
		# Page XML is very picky about not specifying any
		# negative coordinates. we need to clip.
		width, height = self.page.size(False)
		box = shapely.geometry.box(0, 0, width, height)
		poly = shapely.geometry.Polygon(warped_coords).intersection(box)
		return poly.exterior.coords

	def blocks_and_lines(self, block_path):
		blocks = []
		lines = []
		for path in self._mapping[block_path]:
			blocks.append((path, self._input.regions.by_path[path]))
			lines.extend(self._region_lines[path])
		return blocks, dict(lines)

	def _add(self, class_, block_path):
		region = self._regions.get(block_path)
		if region is None:
			region = class_(self, block_path)
			self._regions[block_path] = region
		assert isinstance(region, class_)
		return region

	def get(self, block_path):
		region = self._regions.get(block_path)
		if region is not None:
			return region

		confidences = [
			l.confidence
			for _, l in self._region_lines[block_path]]
		min_confidence = self._input.lines.min_confidence

		if all(c < min_confidence for c in confidences):
			return None
		else:
			raise RuntimeError(
				"no text found for %s, line confidences are: %s" % (
					str(block_path), ", ".join(["%.2f" % x for x in confidences])))

	@property
	def page(self):
		return self._input.page

	@property
	def lines(self):
		return self._input.lines

	@cached_property
	def paths(self):
		return sorted(list(self._regions.keys()))


class RegionReadingOrder:
	def __init__(self, document):
		self._document = document

		self._ordered_regions = []
		self._regionless_text_lines = []

		region_indices = collections.defaultdict(int)
		for p in document.paths:
			region_indices[p[:2]] = max(region_indices[p[:2]], int(p[2]))
		self._region_indices = region_indices

		for path in document.reading_order:
			self.append(path)
		self.close()

	def _flush_regionless_lines(self):
		if not self._regionless_text_lines:
			return

		base_path = self._regionless_text_lines[0][:2]
		assert all(p[:2] == base_path for p in self._regionless_text_lines)

		region_indices = self._region_indices
		new_region_index = region_indices[base_path] + 1
		region_indices[base_path] = new_region_index

		new_region_path = base_path + (str(new_region_index),)
		lines = self._document.lines.by_path
		merged = MergedTextRegion(
			self._document,
			new_region_path,
			[(p, lines[p]) for p in self._regionless_text_lines])
		self._ordered_regions.append((new_region_path, merged))
		self._regionless_text_lines = []

	def _is_adjacent(self, line_path):
		if not self._regionless_text_lines:
			return False

		# did lines originally belong to the same region?
		if self._regionless_text_lines[-1][:3] != line_path[:3]:
			return False

		lines = self._document.lines.by_path
		l0 = lines[self._regionless_text_lines[-1]]
		l1 = lines[line_path]

		# FIXME
		if l0.image_space_polygon.distance(l1.image_space_polygon) < 5:
			return True

		return True

	def _add_regionless_line(self, line_path):
		if not self._is_adjacent(line_path):
			self._flush_regionless_lines()

		self._regionless_text_lines.append(line_path)

	def append(self, path):
		if len(path) == 3:  # block path?
			self._flush_regionless_lines()
			region = self._document.get(path)
			if region is not None:
				self._ordered_regions.append((path, region))
		elif len(path) > 3:  # line path?
			assert path[:2] == ("regions", "TEXT")
			self._add_regionless_line(path)
		else:
			raise ValueError("illegal region/line path %s" % str(path))

	def close(self):
		self._flush_regionless_lines()

	@property
	def reading_order(self):
		return [x[0] for x in self._ordered_regions]

	@property
	def regions(self):
		return [x[1] for x in self._ordered_regions]


class PlainTextComposition:
	def __init__(self, line_separator, block_separator):
		self._line_separator = line_separator
		self._block_separator = block_separator
		self._texts = []
		self._path = None

	def append_text(self, path, text):
		text = text.strip()
		if not text:
			return
		assert isinstance(path, tuple)
		if self._path is not None:
			if path[:3] != self._path[:3]:
				self._texts.append(self._block_separator)
		self._path = path
		self._texts.append(text + "\n")

	@property
	def text(self):
		return "".join(self._texts)


class ComposeProcessor(Processor):
	def __init__(self, options):
		super().__init__(options)
		self._options = options
		self._page_xml = options["page_xml"]

		if options["regions"]:
			self._block_filter = RegionsFilter(options["regions"])
		else:
			self._block_filter = None

		# see https://stackoverflow.com/questions/4020539/
		# process-escape-sequences-in-a-string-in-python
		self._block_separator = codecs.escape_decode(bytes(
			self._options["paragraph"], "utf-8"))[0].decode("utf-8")

	@property
	def processor_name(self):
		return __loader__.name

	def artifacts(self):
		return [
			("input", Input(
				Artifact.CONTOURS,
				Artifact.LINES,
				Artifact.OCR,
				Artifact.ORDER,
				Artifact.TABLES,
				stage=Stage.AGGREGATE)),
			("output", Output(Artifact.COMPOSE)),
		]

	def export_page_xml(self, page_path, document):
		page = document.page

		px_document = pagexml.Document(
			filename=str(page_path),
			image_size=page.warped.size)

		# Page XML does not allow reading orders that
		# contain of regions and lines. We therefore
		# need to merge all line items occurring as
		# separate entities in our reading order into
		# new regions. RegionReadingOrder does that.

		ro = RegionReadingOrder(document)

		px_ro = px_document.append_reading_order()
		px_ro_group = px_ro.append_ordered_group(
			id_="ro_regions", caption="regions reading order")
		for i, path in enumerate(ro.reading_order):
			px_ro_group.append_region_ref_indexed(
				index=i, region_ref="-".join(path))

		for region in ro.regions:
			region.export_page_xml(px_document)

		with io.BytesIO() as f:
			px_document.write(f, overwrite=True, validate=True)
			return f.getvalue()

	def export_plain_text(self, document):
		composition = PlainTextComposition(
			line_separator="\n",
			block_separator=self._block_separator)

		for path in document.reading_order:
			if self._block_filter is not None and not self._block_filter(path):
				continue

			if len(path) == 3:  # is this a block path?
				region = document.get(path)
				if region is not None:
					region.export_plain_text_region(composition)
			elif len(path) == 4:  # is this a line path?
				region = document.get(path[:3])
				if region is not None:
					region.export_plain_text_line(composition, path)
			else:
				raise RuntimeError("illegal path %s in reading order" % path)

		return composition.text

	def process(self, page_path: Path, input, output):
		if not input.regions.by_path:
			return

		document = Document(page_path, input)

		with output.compose() as zf:
			zf.writestr("page.txt", self.export_plain_text(document))
			if self._page_xml:
				zf.writestr("page.xml", self.export_page_xml(page_path, document))


@click.command()
@click.argument(
	'data_path',
	type=click.Path(exists=True),
	required=True)
@click.option(
	'--paragraph',
	type=str,
	default="\n\n",
	help="Character sequence used to separate paragraphs.")
@click.option(
	'--regions',
	type=str,
	default=None,
	help="Only export text from given regions path, e.g. -f \"regions/TEXT\".")
@click.option(
	'--fringe',
	type=float,
	default=0.001)
@click.option(
	'--page-xml',
	is_flag=True,
	default=False)
@Processor.options
def compose(data_path, **kwargs):
	""" Produce text composed in a single text file for each page in DATA_PATH. """
	processor = ComposeProcessor(kwargs)
	processor.traverse(data_path)


if __name__ == "__main__":
	compose()
