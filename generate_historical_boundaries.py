import csv
import datetime
import os
import re
import arcpy

# ---- configuration ----
input_roots = [
    r"P:\IGG\Z_Drive\Cenovus\Imagery\Historicals",
    r"P:\IGG\Z_Drive\Tourmaline\Imagery\Historicals",
    r"P:\IGG\Z_Drive\Whitecap\Imagery\Historicals",
]

target_gdb = r"P:\IGG\Z_Drive\Staging\Historical_Imagery_Staging.gdb"
production_gdb = r"P:\IGG\Z_Drive\Historical_Imagery_Boundary.gdb"
target_feature_class_name = "Historical_Boundary"
client_field_name = "Client"
path_field_name = "Path"
prefixroll_field_name = "PrefixRoll"
photo_field_name = "Photo"
year_field_name = "Year"
date_field_name = "Date"
new_files_csv = r"C:\Users\tblanchard\Documents\Tracy\Code\Historical Imagery\5.1_new_historical_photos.csv"
valid_extensions = {".tif", ".tiff"}


def parse_tiff_name(file_path):
    """Extract PrefixRoll, Photo, Year, Date from stem; missing parts are None/empty."""
    stem = os.path.splitext(os.path.basename(file_path))[0]

    year = None
    date_value = None
    date_text = ""

    paren_match = re.search(r"\((\d{4})(?:-(\d{2})-(\d{2}))?\)\s*$", stem)
    if paren_match:
        base = stem[:paren_match.start()].rstrip()
        year = paren_match.group(1)
        if paren_match.group(2) and paren_match.group(3):
            month_str = paren_match.group(2)
            day_str = paren_match.group(3)
            try:
                date_value = datetime.datetime(int(year), int(month_str), int(day_str))
                date_text = f"{year}-{month_str}-{day_str}"
            except ValueError:
                pass
    else:
        base = stem

    hyphen_idx = base.rfind("-")
    if hyphen_idx != -1:
        prefixroll = base[:hyphen_idx].strip()
        photo = base[hyphen_idx + 1:].strip()
    else:
        prefixroll = base.strip()
        photo = ""

    return {
        "prefixroll": prefixroll,
        "photo": photo,
        "year": year,
        "date": date_value,
        "date_text": date_text,
    }


def derive_client_from_path(file_path):
    """Derive client name from the folder immediately before 'Imagery'."""
    normalized_path = os.path.normpath(file_path)
    parts = normalized_path.split(os.sep)

    for idx, part in enumerate(parts):
        if part.lower() == "imagery" and idx > 0:
            return parts[idx - 1]

    return ""


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


def iter_tiff_files(root_dir):
    """Yield TIFF file paths recursively from root_dir."""
    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            if ext in valid_extensions:
                yield os.path.join(dirpath, filename)


def get_existing_paths(feature_class, path_field):
    """Read all existing paths from the feature class to avoid duplicates."""
    existing_paths = set()
    try:
        with arcpy.da.SearchCursor(feature_class, [path_field]) as s_cur:
            for row in s_cur:
                if row[0]:
                    existing_paths.add(row[0].lower())
    except Exception as ex:
        print(f"[WARNING] Error reading existing paths from feature class: {ex}")
    return existing_paths


def is_lock_error(ex):
    """Best-effort detection for lock-related ArcPy errors."""
    error_text = str(ex).lower()
    return "lock" in error_text


if __name__ == "__main__":
    arcpy.env.overwriteOutput = True

    if not arcpy.Exists(target_gdb):
        raise RuntimeError(f"Target geodatabase not found: {target_gdb}")

    target_fc = os.path.join(target_gdb, target_feature_class_name)
    if not arcpy.Exists(target_fc):
        raise RuntimeError(f"Target feature class not found: {target_fc}")

    field_names = [f.name.lower() for f in arcpy.ListFields(target_fc)]
    required_fields = [
        client_field_name,
        path_field_name,
        prefixroll_field_name,
        photo_field_name,
        year_field_name,
        date_field_name,
    ]
    for required_field in required_fields:
        if required_field.lower() not in field_names:
            raise RuntimeError(f"Required field not found in {target_fc}: {required_field}")

    target_sr = arcpy.Describe(target_fc).spatialReference
    year_field = [f for f in arcpy.ListFields(target_fc) if f.name.lower() == year_field_name.lower()][0]
    date_field = [f for f in arcpy.ListFields(target_fc) if f.name.lower() == date_field_name.lower()][0]
    year_is_numeric = year_field.type in {"SmallInteger", "Integer", "Single", "Double"}
    date_is_date_type = date_field.type == "Date"

    print(f"Target feature class: {target_fc}")
    print(f"Target CRS: {target_sr.name}")

    # Load existing paths from the feature class
    print("Loading existing paths from feature class...")
    existing_paths = get_existing_paths(target_fc, path_field_name)
    print(f"Found {len(existing_paths)} existing paths in feature class")

    scanned_tiffs = 0
    missing_roots = 0
    duplicate_scan_filenames = 0
    inserted = 0
    skipped_unknown_client = 0
    skipped_invalid = 0
    skipped_existing = 0
    production_inserted = 0
    production_skipped_existing = 0
    production_skipped_locked = 0
    production_skipped_error = 0

    discovered_by_path = {}
    staging_insert_rows = []
    csv_rows = []
    csv_rows_by_path = {}

    for root in input_roots:
        if not os.path.isdir(root):
            missing_roots += 1
            print(f"[SKIP][MISSING ROOT] {root}")
            continue

        print(f"Scanning: {root}")
        for tif_path in iter_tiff_files(root):
            scanned_tiffs += 1
            path_key = tif_path.lower()

            # Skip if already in feature class
            if path_key in existing_paths:
                skipped_existing += 1
                continue

            if path_key in discovered_by_path:
                duplicate_scan_filenames += 1
                continue

            discovered_by_path[path_key] = (os.path.basename(tif_path), tif_path, root)

    insert_fields = [
        "SHAPE@",
        client_field_name,
        path_field_name,
        prefixroll_field_name,
        photo_field_name,
        year_field_name,
        date_field_name,
    ]

    print(f"Processing {len(discovered_by_path)} new files...")

    with arcpy.da.InsertCursor(target_fc, insert_fields) as i_cur:
        for path_key, (file_name, tif_path, source_root) in discovered_by_path.items():
            parsed_name = parse_tiff_name(tif_path)
            client_value = derive_client_from_path(tif_path)

            if not client_value:
                skipped_unknown_client += 1
                csv_rows.append([file_name, tif_path, "SKIP_UNKNOWN_CLIENT", "NOT_ATTEMPTED"])
                print(f"[SKIP][UNKNOWN CLIENT] {tif_path}")
                continue

            if not os.path.isfile(tif_path):
                skipped_invalid += 1
                csv_rows.append([file_name, tif_path, "SKIP_INVALID_PATH", "NOT_ATTEMPTED"])
                print(f"[SKIP][INVALID TIFF] {tif_path}")
                continue

            try:
                desc = arcpy.Describe(tif_path)
                extent = desc.extent
                src_sr = desc.spatialReference

                if extent is None:
                    skipped_invalid += 1
                    csv_rows.append([file_name, tif_path, "SKIP_NO_EXTENT", "NOT_ATTEMPTED"])
                    print(f"[SKIP][NO EXTENT] {tif_path}")
                    continue

                if src_sr is None or src_sr.name.lower() == "unknown":
                    skipped_invalid += 1
                    csv_rows.append([file_name, tif_path, "SKIP_UNKNOWN_CRS", "NOT_ATTEMPTED"])
                    print(f"[SKIP][UNKNOWN CRS] {tif_path}")
                    continue

                poly = make_extent_polygon(extent, src_sr)
                if src_sr.factoryCode != target_sr.factoryCode:
                    poly = poly.projectAs(target_sr)

                insert_year = (int(parsed_name["year"]) if year_is_numeric else parsed_name["year"]) if parsed_name["year"] is not None else None
                insert_date = (parsed_name["date"] if date_is_date_type else (parsed_name["date_text"] or None)) if parsed_name["date"] is not None else None
                i_cur.insertRow(
                    [
                        poly,
                        client_value,
                        tif_path,
                        parsed_name["prefixroll"],
                        parsed_name["photo"],
                        insert_year,
                        insert_date,
                    ]
                )
                inserted += 1
                staging_insert_rows.append(
                    {
                        "path_key": path_key,
                        "path_value": tif_path,
                        "row_values": [
                            poly,
                            client_value,
                            tif_path,
                            parsed_name["prefixroll"],
                            parsed_name["photo"],
                            insert_year,
                            insert_date,
                        ],
                    }
                )
                csv_rows.append([file_name, tif_path, "INSERTED", "PENDING"])
                csv_rows_by_path[path_key] = len(csv_rows) - 1

            except Exception as ex:
                skipped_invalid += 1
                csv_rows.append([file_name, tif_path, "SKIP_ERROR", "NOT_ATTEMPTED"])
                print(f"[SKIP][ERROR] {tif_path}: {ex}")

    production_fc = os.path.join(production_gdb, target_feature_class_name)
    if staging_insert_rows:
        print(f"Syncing {len(staging_insert_rows)} new staging rows to production: {production_fc}")

        if not arcpy.Exists(production_gdb):
            production_skipped_error += len(staging_insert_rows)
            for inserted_row in staging_insert_rows:
                idx = csv_rows_by_path.get(inserted_row["path_key"])
                if idx is not None:
                    csv_rows[idx][3] = "NOT_ATTEMPTED"
            print(f"[WARNING] Production geodatabase not found. Skipping sync: {production_gdb}")
        elif not arcpy.Exists(production_fc):
            production_skipped_error += len(staging_insert_rows)
            for inserted_row in staging_insert_rows:
                idx = csv_rows_by_path.get(inserted_row["path_key"])
                if idx is not None:
                    csv_rows[idx][3] = "NOT_ATTEMPTED"
            print(f"[WARNING] Production feature class not found. Skipping sync: {production_fc}")
        else:
            try:
                production_existing_paths = get_existing_paths(production_fc, path_field_name)
                rows_to_insert_production = []
                for inserted_row in staging_insert_rows:
                    idx = csv_rows_by_path.get(inserted_row["path_key"])
                    if inserted_row["path_key"] in production_existing_paths:
                        production_skipped_existing += 1
                        if idx is not None:
                            csv_rows[idx][3] = "SKIP_EXISTING"
                    else:
                        rows_to_insert_production.append(inserted_row)

                if rows_to_insert_production:
                    try:
                        with arcpy.da.InsertCursor(production_fc, insert_fields) as p_cur:
                            for inserted_row in rows_to_insert_production:
                                idx = csv_rows_by_path.get(inserted_row["path_key"])
                                try:
                                    p_cur.insertRow(inserted_row["row_values"])
                                    production_inserted += 1
                                    if idx is not None:
                                        csv_rows[idx][3] = "INSERTED"
                                except Exception as ex:
                                    if is_lock_error(ex):
                                        production_skipped_locked += 1
                                        if idx is not None:
                                            csv_rows[idx][3] = "SKIP_LOCK"
                                        print(f"[SKIP][PRODUCTION LOCK] {inserted_row['path_value']}: {ex}")
                                    else:
                                        production_skipped_error += 1
                                        if idx is not None:
                                            csv_rows[idx][3] = "SKIP_ERROR"
                                        print(f"[SKIP][PRODUCTION ERROR] {inserted_row['path_value']}: {ex}")
                    except Exception as ex:
                        if is_lock_error(ex):
                            production_skipped_locked += len(rows_to_insert_production)
                            for inserted_row in rows_to_insert_production:
                                idx = csv_rows_by_path.get(inserted_row["path_key"])
                                if idx is not None:
                                    csv_rows[idx][3] = "SKIP_LOCK"
                            print(f"[WARNING] Production insert skipped due to lock: {ex}")
                        else:
                            production_skipped_error += len(rows_to_insert_production)
                            for inserted_row in rows_to_insert_production:
                                idx = csv_rows_by_path.get(inserted_row["path_key"])
                                if idx is not None:
                                    csv_rows[idx][3] = "SKIP_ERROR"
                            print(f"[WARNING] Production insert skipped due to error: {ex}")
            except Exception as ex:
                if is_lock_error(ex):
                    production_skipped_locked += len(staging_insert_rows)
                    for inserted_row in staging_insert_rows:
                        idx = csv_rows_by_path.get(inserted_row["path_key"])
                        if idx is not None:
                            csv_rows[idx][3] = "SKIP_LOCK"
                    print(f"[WARNING] Production sync skipped due to lock while reading existing paths: {ex}")
                else:
                    production_skipped_error += len(staging_insert_rows)
                    for inserted_row in staging_insert_rows:
                        idx = csv_rows_by_path.get(inserted_row["path_key"])
                        if idx is not None:
                            csv_rows[idx][3] = "SKIP_ERROR"
                    print(f"[WARNING] Production sync skipped due to error while reading existing paths: {ex}")

    with open(new_files_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["filename", "tiff_path", "staging_status", "final_status"])
        writer.writerows(csv_rows)

    print("\nDone")
    print(f"Roots configured: {len(input_roots)}")
    print(f"Missing roots: {missing_roots}")
    print(f"TIFF files scanned: {scanned_tiffs}")
    print(f"Existing paths skipped: {skipped_existing}")
    print(f"Duplicate filenames found during scan: {duplicate_scan_filenames}")
    print(f"New files discovered: {len(discovered_by_path)}")
    print(f"Features inserted: {inserted}")
    print(f"Skipped unknown client in path: {skipped_unknown_client}")
    print(f"Skipped invalid/error: {skipped_invalid}")
    print(f"Production features inserted: {production_inserted}")
    print(f"Production existing paths skipped: {production_skipped_existing}")
    print(f"Production rows skipped due to lock: {production_skipped_locked}")
    print(f"Production rows skipped due to errors: {production_skipped_error}")
    print(f"CSV output: {new_files_csv}")
