import csv
import os
import re
import arcpy

# ---- configuration ----
csv_file = r"C:\Users\tblanchard\Documents\Tracy\Code\Historical Imagery\renamed_tiff_paths.csv"
target_gdb = r"P:\IGG\Z_Drive\Historical_Imagery_Boundary.gdb"
target_feature_class_name = "Historical_Boundary"
path_column = "edited_tiff_path"
path_field_name = "Path"
year_field_name = "Year"


def extract_year_from_filename(file_path):
	"""Extract 4-digit year (19xx/20xx) from TIFF file name stem."""
	stem = os.path.splitext(os.path.basename(file_path))[0]
	match = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", stem)
	return match.group(0) if match else ""


def make_extent_polygon(extent, spatial_ref):
	"""Create a polygon geometry from an extent in the given spatial reference."""
	arr = arcpy.Array(
		[
			arcpy.Point(extent.XMin, extent.YMin),
			arcpy.Point(extent.XMin, extent.YMax),
			arcpy.Point(extent.XMax, extent.YMax),
			arcpy.Point(extent.XMax, extent.YMin),
			arcpy.Point(extent.XMin, extent.YMin),
		]
	)
	return arcpy.Polygon(arr, spatial_ref)


if __name__ == "__main__":
	arcpy.env.overwriteOutput = True

	if not os.path.exists(csv_file):
		raise FileNotFoundError(f"Input CSV not found: {csv_file}")
	if not arcpy.Exists(target_gdb):
		raise RuntimeError(f"Target geodatabase not found: {target_gdb}")

	target_fc = os.path.join(target_gdb, target_feature_class_name)
	if not arcpy.Exists(target_fc):
		raise RuntimeError(f"Target feature class not found: {target_fc}")

	field_names = [f.name.lower() for f in arcpy.ListFields(target_fc)]
	if path_field_name.lower() not in field_names:
		raise RuntimeError(f"Required field not found in {target_fc}: {path_field_name}")
	if year_field_name.lower() not in field_names:
		raise RuntimeError(f"Required field not found in {target_fc}: {year_field_name}")

	target_sr = arcpy.Describe(target_fc).spatialReference
	year_field = [f for f in arcpy.ListFields(target_fc) if f.name.lower() == year_field_name.lower()][0]
	year_is_numeric = year_field.type in {"SmallInteger", "Integer", "Single", "Double"}

	print(f"Target feature class: {target_fc}")
	print(f"Target CRS: {target_sr.name}")

	# Load existing path values to avoid duplicates.
	existing_paths = set()
	with arcpy.da.SearchCursor(target_fc, [path_field_name]) as s_cur:
		for row in s_cur:
			if row[0]:
				existing_paths.add(row[0])

	tiffs_read = 0
	inserted = 0
	skipped_existing = 0
	skipped_no_year = 0
	skipped_invalid = 0

	with open(csv_file, newline="", encoding="utf-8") as f:
		reader = csv.DictReader(f)

		with arcpy.da.InsertCursor(target_fc, ["SHAPE@", path_field_name, year_field_name]) as i_cur:
			for row_idx, row in enumerate(reader, start=2):
				tif_path = (row.get(path_column) or "").strip()
				if not tif_path:
					continue

				tiffs_read += 1

				if not os.path.isfile(tif_path):
					skipped_invalid += 1
					print(f"[SKIP][INVALID TIFF] Row {row_idx}: {tif_path}")
					continue

				if tif_path in existing_paths:
					skipped_existing += 1
					continue

				year_value = extract_year_from_filename(tif_path)
				if not year_value:
					skipped_no_year += 1
					print(f"[SKIP][NO YEAR IN NAME] {tif_path}")
					continue

				try:
					desc = arcpy.Describe(tif_path)
					extent = desc.extent
					src_sr = desc.spatialReference

					if extent is None:
						skipped_invalid += 1
						print(f"[SKIP][NO EXTENT] {tif_path}")
						continue

					if src_sr is None or src_sr.name.lower() == "unknown":
						skipped_invalid += 1
						print(f"[SKIP][UNKNOWN CRS] {tif_path}")
						continue

					poly = make_extent_polygon(extent, src_sr)
					if src_sr.factoryCode != target_sr.factoryCode:
						poly = poly.projectAs(target_sr)

					insert_year = int(year_value) if year_is_numeric else year_value
					i_cur.insertRow([poly, tif_path, insert_year])
					existing_paths.add(tif_path)
					inserted += 1

				except Exception as ex:
					skipped_invalid += 1
					print(f"[SKIP][ERROR] {tif_path}: {ex}")

	print("\nDone")
	print(f"TIFF paths read from CSV: {tiffs_read}")
	print(f"Features inserted: {inserted}")
	print(f"Skipped existing Path values: {skipped_existing}")
	print(f"Skipped no year in file name: {skipped_no_year}")
	print(f"Skipped invalid/error: {skipped_invalid}")
