import csv
import datetime
import os
import re
import sys
import time

from historical_run_report import HistoricalRunReport

reports_dir = r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\Historical_Imagery_Reports"
new_files_csv = os.path.join(reports_dir, "5.1_new_historical_photos.csv")
run_report = HistoricalRunReport(__file__, os.path.dirname(new_files_csv))
print(f"Detailed diagnostics: {run_report.path}")

try:
    import arcpy
except Exception as error:
    run_report.exception("import_arcpy", error)
    run_report.close()
    raise


def _uncaught_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    try:
        run_report.exception("run_fatal", exc_value)
        print(f"Fatal error; detailed diagnostics: {run_report.path}")
    finally:
        run_report.close()
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


sys.excepthook = _uncaught_exception

# ---- configuration ----
input_roots = [
    r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Client\Cenovus\Imagery\Historicals",
    r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Client\Tourmaline\Imagery\Historicals",
    r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Client\Whitecap\Imagery\Historicals",
]

target_gdb = r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\Historical_Imagery_Staging.gdb"
production_gdb = r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Geodatabase\Historical_Imagery_Boundary.gdb"
target_feature_class_name = "Historical_Boundary"
client_field_name = "Client"
path_field_name = "Path"
prefixroll_field_name = "PrefixRoll"
photo_field_name = "Photo"
year_field_name = "Year"
date_field_name = "Date"
valid_extensions = {".tif", ".tiff"}


def walk_error(error):
    message = f"Could not scan directory {error.filename}: {error}"
    print(f"[WARNING] {message}")
    run_report.record("scan_directory", "warning", input_path=error.filename or "",
                      message=message, details=repr(error))


def parse_tiff_name(file_path):
    """Extract PrefixRoll, Photo, Year, Date from stem; missing parts are None/empty."""
    stem = os.path.splitext(os.path.basename(file_path))[0]

    year = None
    date_value = None
    date_text = ""
    invalid_date = False

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
                invalid_date = True
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
        "invalid_date": invalid_date,
    }


def validate_tiff_name(parsed_name):
    """Require at least a non-empty PrefixRoll and trailing four-digit year."""
    if not parsed_name["prefixroll"]:
        return "missing PrefixRoll"
    if parsed_name["year"] is None:
        return "missing trailing year in parentheses; expected at least Prefix(yyyy)"
    if parsed_name["invalid_date"]:
        return "invalid calendar date in (yyyy-mm-dd) suffix"
    return None


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


def select_geographic_transformation(source_sr, target_sr, extent):
    """Choose ArcGIS's top-ranked transformation, or report when one is required but unavailable."""
    transformations = arcpy.ListTransformations(
        source_sr,
        target_sr,
        extent,
        first_only=True,
    )
    if transformations:
        return transformations[0], "ArcGIS top-ranked applicable transformation"

    source_gcs = source_sr.GCS
    target_gcs = target_sr.GCS
    source_datum_code = source_gcs.datumCode
    target_datum_code = target_gcs.datumCode
    if source_datum_code and target_datum_code:
        same_datum = source_datum_code == target_datum_code
    else:
        same_datum = source_gcs.datumName.casefold() == target_gcs.datumName.casefold()

    if same_datum:
        return None, "No datum transformation required; geographic datums match"

    return None, (
        "Source and target geographic datums differ, but ArcGIS found no applicable "
        "transformation for this raster extent"
    )


def iter_tiff_files(root_dir):
    """Yield TIFF file paths recursively from root_dir."""
    for dirpath, _, filenames in os.walk(root_dir, onerror=walk_error):
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
                    existing_paths.add(normalize_path(row[0]))
    except Exception as ex:
        run_report.exception("read_existing_paths", ex, input_path=feature_class)
        raise
    return existing_paths


def get_existing_rows_by_path(feature_class, fields, path_index):
    """Read feature rows keyed by their normalized path value."""
    rows_by_path = {}
    try:
        with arcpy.da.SearchCursor(feature_class, fields) as s_cur:
            for row in s_cur:
                if row[path_index]:
                    rows_by_path[normalize_path(row[path_index])] = list(row)
    except Exception as ex:
        run_report.exception("read_existing_staging_rows", ex, input_path=feature_class)
        raise
    return rows_by_path


def normalize_path(file_path):
    """Normalize path spelling for comparisons across feature classes and scans."""
    return os.path.normcase(os.path.normpath(str(file_path).replace("/", "\\")))


def is_lock_error(ex):
    """Best-effort detection for lock-related ArcPy errors."""
    error_text = str(ex).lower()
    return "lock" in error_text


if __name__ == "__main__":
    started_at = time.perf_counter()
    run_report.record("run", "started", message="Historical boundary generation started",
                      details=(f"input_roots={input_roots}; staging_gdb={target_gdb}; "
                               f"production_gdb={production_gdb}"))

    try:
        arcpy.env.overwriteOutput = True
        install_info = arcpy.GetInstallInfo()
        run_report.record("configure_arcpy", "completed",
                          message=f"ArcGIS Pro {install_info.get('Version', 'unknown')}; product={arcpy.ProductInfo()}")
    except Exception as ex:
        run_report.exception("configure_arcpy", ex)
        raise

    if not arcpy.Exists(target_gdb):
        raise RuntimeError(f"Target geodatabase not found: {target_gdb}")

    target_fc = os.path.join(target_gdb, target_feature_class_name)
    if not arcpy.Exists(target_fc):
        raise RuntimeError(f"Target feature class not found: {target_fc}")

    production_fc = os.path.join(production_gdb, target_feature_class_name)
    if not arcpy.Exists(production_gdb):
        raise RuntimeError(f"Production geodatabase not found: {production_gdb}")
    if not arcpy.Exists(production_fc):
        raise RuntimeError(f"Production feature class not found: {production_fc}")

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
    production_field_names = [f.name.lower() for f in arcpy.ListFields(production_fc)]
    for required_field in required_fields:
        if required_field.lower() not in production_field_names:
            raise RuntimeError(f"Required field not found in {production_fc}: {required_field}")

    run_report.record("validate_schema", "completed", output_path=f"{target_fc}; {production_fc}",
                      message="Staging and production feature classes contain all required fields")
    run_report.record("validate_schema", "info", output_path=f"{target_fc}; {production_fc}",
                      message="Write availability will be determined by the actual feature insert operations")

    target_sr = arcpy.Describe(target_fc).spatialReference
    year_field = [f for f in arcpy.ListFields(target_fc) if f.name.lower() == year_field_name.lower()][0]
    date_field = [f for f in arcpy.ListFields(target_fc) if f.name.lower() == date_field_name.lower()][0]
    year_is_numeric = year_field.type in {"SmallInteger", "Integer", "Single", "Double"}
    date_is_date_type = date_field.type == "Date"

    print(f"Target feature class: {target_fc}")
    print(f"Target CRS: {target_sr.name}")

    # Production is authoritative for deciding whether a TIFF is already complete.
    print("Loading existing paths from production feature class...")
    run_report.record("read_existing_features", "started", output_path=production_fc,
                      message="Reading production and staging feature class paths")
    existing_paths = get_existing_paths(production_fc, path_field_name)
    staging_row_fields = [
        "SHAPE@",
        client_field_name,
        path_field_name,
        prefixroll_field_name,
        photo_field_name,
        year_field_name,
        date_field_name,
    ]
    staging_rows_by_path = get_existing_rows_by_path(target_fc, staging_row_fields, 2)
    staging_paths = set(staging_rows_by_path)
    print(f"Found {len(existing_paths)} existing production paths")
    print(f"Found {len(staging_paths)} existing staging paths")
    run_report.record("read_existing_features", "completed", output_path=production_fc,
                      message=f"Production paths={len(existing_paths)}; staging paths={len(staging_paths)}",
                      details=f"Staging feature class: {target_fc}")

    scanned_tiffs = 0
    missing_roots = 0
    duplicate_scan_filenames = 0
    inserted = 0
    skipped_unknown_client = 0
    skipped_invalid = 0
    skipped_invalid_name = 0
    skipped_missing_transformation = 0
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
            run_report.record("check_input_root", "warning", input_path=root,
                              message="Historical imagery input root is missing or inaccessible")
            continue

        print(f"Scanning: {root}")
        run_report.record("scan_input_root", "started", input_path=root, message="Recursive TIFF scan started")
        root_scan_start = time.perf_counter()
        root_scan_count = 0
        for tif_path in iter_tiff_files(root):
            scanned_tiffs += 1
            root_scan_count += 1
            if root_scan_count == 1 or root_scan_count % 50 == 0:
                run_report.record(
                    "scan_input_root", "progress", input_path=root,
                    message=f"Scanned {root_scan_count} TIFF(s) in this root; {scanned_tiffs} total",
                    details=f"elapsed_seconds={time.perf_counter() - root_scan_start:.1f}",
                )
            path_key = normalize_path(tif_path)

            # Skip if already in feature class
            if path_key in existing_paths:
                skipped_existing += 1
                csv_rows.append([os.path.basename(tif_path), tif_path, "SKIP_EXISTING", "SKIP_EXISTING"])
                run_report.record("duplicate_check", "skipped", client=derive_client_from_path(tif_path),
                                  input_path=tif_path, message="Path already exists in production")
                continue

            if path_key in discovered_by_path:
                duplicate_scan_filenames += 1
                run_report.record("duplicate_scan_path", "warning", client=derive_client_from_path(tif_path),
                                  input_path=tif_path,
                                  message="Same normalized TIFF path was discovered more than once")
                continue

            discovered_by_path[path_key] = (os.path.basename(tif_path), tif_path, root)
        run_report.record("scan_input_root", "completed", input_path=root,
                          message="Recursive TIFF scan completed",
                          details=f"elapsed_seconds={time.perf_counter() - root_scan_start:.2f}")

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
    run_report.record("process_discovered_tiffs", "started", message=f"Processing {len(discovered_by_path)} candidate TIFF(s)")

    with arcpy.da.InsertCursor(target_fc, insert_fields) as i_cur:
        for path_key, (file_name, tif_path, source_root) in discovered_by_path.items():
            file_start = time.perf_counter()
            run_report.record("process_tiff", "started", input_path=tif_path,
                              message="Validating raster metadata and preparing staging boundary")
            parsed_name = parse_tiff_name(tif_path)
            client_value = derive_client_from_path(tif_path)

            if not client_value:
                skipped_unknown_client += 1
                csv_rows.append([file_name, tif_path, "SKIP_UNKNOWN_CLIENT", "NOT_ATTEMPTED"])
                print(f"[SKIP][UNKNOWN CLIENT] {tif_path}")
                run_report.record("validate_client", "warning", input_path=tif_path,
                                  message="Could not derive client from path")
                continue

            if not os.path.isfile(tif_path):
                skipped_invalid += 1
                csv_rows.append([file_name, tif_path, "SKIP_INVALID_PATH", "NOT_ATTEMPTED"])
                print(f"[SKIP][INVALID TIFF] {tif_path}")
                run_report.record("validate_file", "warning", client=client_value,
                                  input_path=tif_path, message="TIFF path is not a readable file")
                continue

            name_error = validate_tiff_name(parsed_name)
            if name_error:
                skipped_invalid += 1
                skipped_invalid_name += 1
                csv_rows.append([file_name, tif_path, "SKIP_INVALID_NAME", "NOT_ATTEMPTED"])
                print(f"[SKIP][INVALID NAME] {tif_path}: {name_error}")
                run_report.record("validate_filename", "warning", client=client_value,
                                  input_path=tif_path, message=name_error)
                continue

            try:
                desc = arcpy.Describe(tif_path)
                extent = desc.extent
                src_sr = desc.spatialReference

                if extent is None:
                    skipped_invalid += 1
                    csv_rows.append([file_name, tif_path, "SKIP_NO_EXTENT", "NOT_ATTEMPTED"])
                    print(f"[SKIP][NO EXTENT] {tif_path}")
                    run_report.record("inspect_tiff", "warning", client=client_value,
                                      input_path=tif_path, message="Raster has no extent")
                    continue

                if src_sr is None or src_sr.name.lower() == "unknown":
                    skipped_invalid += 1
                    csv_rows.append([file_name, tif_path, "SKIP_UNKNOWN_CRS", "NOT_ATTEMPTED"])
                    print(f"[SKIP][UNKNOWN CRS] {tif_path}")
                    run_report.record("inspect_tiff", "warning", client=client_value,
                                      input_path=tif_path, message="Raster has unknown CRS")
                    continue

                transformation, transformation_reason = select_geographic_transformation(
                    src_sr, target_sr, extent
                )
                source_gcs = src_sr.GCS
                target_gcs = target_sr.GCS
                source_datum_code = source_gcs.datumCode
                target_datum_code = target_gcs.datumCode
                datums_differ = (
                    source_datum_code != target_datum_code
                    if source_datum_code and target_datum_code
                    else source_gcs.datumName.casefold() != target_gcs.datumName.casefold()
                )
                if datums_differ and transformation is None:
                    skipped_invalid += 1
                    skipped_missing_transformation += 1
                    csv_rows.append([file_name, tif_path, "SKIP_NO_TRANSFORMATION", "NOT_ATTEMPTED"])
                    message = "Different source/target datums, but no applicable geographic transformation is available"
                    print(f"[SKIP][NO TRANSFORMATION] {tif_path}: {message}")
                    run_report.record(
                        "select_geographic_transformation", "warning", client=client_value,
                        input_path=tif_path, message=message,
                        details=(f"source_crs={src_sr.name} (EPSG:{src_sr.factoryCode}); "
                                 f"source_datum={source_gcs.datumName} ({source_datum_code}); "
                                 f"target_crs={target_sr.name} (EPSG:{target_sr.factoryCode}); "
                                 f"target_datum={target_gcs.datumName} ({target_datum_code})"),
                    )
                    continue

                transformation_log = transformation or "NONE (same geographic datum)"
                print(f"[CRS] {src_sr.name} -> {target_sr.name}; transformation={transformation_log}")
                run_report.record(
                    "select_geographic_transformation", "info", client=client_value,
                    input_path=tif_path, output_path=target_fc,
                    message=transformation_reason,
                    details=(f"transformation={transformation_log}; source_gcs={source_gcs.name}; "
                             f"target_gcs={target_gcs.name}"),
                )

                if path_key in staging_rows_by_path:
                    staging_insert_rows.append(
                        {
                            "path_key": path_key,
                            "path_value": tif_path,
                            "row_values": staging_rows_by_path[path_key],
                        }
                    )
                    csv_rows.append([file_name, tif_path, "STAGING_EXISTING", "PENDING"])
                    csv_rows_by_path[path_key] = len(csv_rows) - 1
                    run_report.record("staging_feature", "info", client=client_value,
                                      input_path=tif_path, output_path=target_fc,
                                      message="Staging path exists; queued for production synchronization")
                    continue

                poly = make_extent_polygon(extent, src_sr)
                poly = poly.projectAs(target_sr, transformation)

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
                run_report.record("insert_staging_feature", "completed", client=client_value,
                                  input_path=tif_path, output_path=target_fc,
                                  message="Boundary feature inserted into staging",
                                  details=f"elapsed_seconds={time.perf_counter() - file_start:.2f}")

            except Exception as ex:
                skipped_invalid += 1
                csv_rows.append([file_name, tif_path, "SKIP_ERROR", "NOT_ATTEMPTED"])
                print(f"[SKIP][ERROR] {tif_path}: {ex}")
                run_report.exception("process_tiff_or_insert_staging", ex, client=client_value,
                                     input_path=tif_path, output_path=target_fc,
                                     details=arcpy.GetMessages(2) if hasattr(arcpy, "GetMessages") else "")

    if staging_insert_rows:
        print(f"Syncing {len(staging_insert_rows)} new staging rows to production: {production_fc}")

        try:
                production_existing_paths = get_existing_paths(production_fc, path_field_name)
                rows_to_insert_production = []
                for inserted_row in staging_insert_rows:
                    idx = csv_rows_by_path.get(inserted_row["path_key"])
                    if inserted_row["path_key"] in production_existing_paths:
                        production_skipped_existing += 1
                        if idx is not None:
                            csv_rows[idx][3] = "SKIP_EXISTING"
                        run_report.record("production_duplicate_check", "skipped",
                                          input_path=inserted_row["path_value"], output_path=production_fc,
                                          message="Path already exists in production feature class")
                    else:
                        rows_to_insert_production.append(inserted_row)

                if rows_to_insert_production:
                    production_inserted_before_cursor = production_inserted
                    production_locked_before_cursor = production_skipped_locked
                    production_errors_before_cursor = production_skipped_error
                    try:
                        with arcpy.da.InsertCursor(production_fc, insert_fields) as p_cur:
                            for inserted_row in rows_to_insert_production:
                                idx = csv_rows_by_path.get(inserted_row["path_key"])
                                try:
                                    p_cur.insertRow(inserted_row["row_values"])
                                    production_inserted += 1
                                    if idx is not None:
                                        csv_rows[idx][3] = "INSERTED"
                                    run_report.record("insert_production_feature", "completed",
                                                      input_path=inserted_row["path_value"],
                                                      output_path=production_fc,
                                                      message="Boundary feature inserted into production")
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
                                    run_report.exception("insert_production_feature", ex,
                                                         input_path=inserted_row["path_value"],
                                                         output_path=production_fc,
                                                         details=arcpy.GetMessages(2))
                    except Exception as ex:
                        # InsertCursor may raise while opening or while closing/committing.
                        # Do not label every row failed until checking which inserts persisted.
                        error_details = arcpy.GetMessages(2)
                        print(f"[WARNING] Production insert cursor failed: {ex}")
                        run_report.exception("production_insert_cursor_lifecycle", ex,
                                             output_path=production_fc,
                                             details=error_details)
                        production_inserted = production_inserted_before_cursor
                        production_skipped_locked = production_locked_before_cursor
                        production_skipped_error = production_errors_before_cursor
                        try:
                            production_paths_after_cursor = get_existing_paths(
                                production_fc, path_field_name
                            )
                        except Exception as verify_error:
                            run_report.exception("verify_production_after_cursor_error", verify_error,
                                                 output_path=production_fc,
                                                 details=arcpy.GetMessages(2))
                            for inserted_row in rows_to_insert_production:
                                idx = csv_rows_by_path.get(inserted_row["path_key"])
                                if idx is not None:
                                    csv_rows[idx][3] = "VERIFY_REQUIRED"
                            production_skipped_error += len(rows_to_insert_production)
                        else:
                            for inserted_row in rows_to_insert_production:
                                idx = csv_rows_by_path.get(inserted_row["path_key"])
                                if inserted_row["path_key"] in production_paths_after_cursor:
                                    production_inserted += 1
                                    if idx is not None:
                                        csv_rows[idx][3] = "INSERTED_VERIFIED_AFTER_CURSOR_ERROR"
                                    run_report.record(
                                        "verify_production_after_cursor_error", "completed",
                                        input_path=inserted_row["path_value"],
                                        output_path=production_fc,
                                        message="Path exists in production despite InsertCursor lifecycle error",
                                    )
                                else:
                                    if is_lock_error(ex):
                                        production_skipped_locked += 1
                                        final_status = "SKIP_LOCK"
                                    else:
                                        production_skipped_error += 1
                                        final_status = "SKIP_ERROR"
                                    if idx is not None:
                                        csv_rows[idx][3] = final_status
                                    run_report.record(
                                        "verify_production_after_cursor_error", "failed",
                                        issue_category="schema_lock" if final_status == "SKIP_LOCK" else "geoprocessing",
                                        input_path=inserted_row["path_value"],
                                        output_path=production_fc,
                                        message=f"Path not found in production after cursor error: {ex}",
                                        details=error_details,
                                    )
        except Exception as ex:
            if is_lock_error(ex):
                production_skipped_locked += len(staging_insert_rows)
                for inserted_row in staging_insert_rows:
                    idx = csv_rows_by_path.get(inserted_row["path_key"])
                    if idx is not None:
                        csv_rows[idx][3] = "SKIP_LOCK"
                print(f"[WARNING] Production sync skipped due to lock while reading existing paths: {ex}")
                run_report.exception("read_production_paths_for_sync", ex, output_path=production_fc,
                                     details=arcpy.GetMessages(2))
            else:
                production_skipped_error += len(staging_insert_rows)
                for inserted_row in staging_insert_rows:
                    idx = csv_rows_by_path.get(inserted_row["path_key"])
                    if idx is not None:
                        csv_rows[idx][3] = "SKIP_ERROR"
                print(f"[WARNING] Production sync skipped due to error while reading existing paths: {ex}")
                run_report.exception("read_production_paths_for_sync", ex, output_path=production_fc,
                                     details=arcpy.GetMessages(2))

    with open(new_files_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["filename", "tiff_path", "staging_status", "final_status"])
        writer.writerows(csv_rows)
    run_report.record("write_operational_csv", "completed", output_path=new_files_csv,
                      message=f"Wrote {len(csv_rows)} per-file result row(s)")

    print("\nDone")
    print(f"Roots configured: {len(input_roots)}")
    print(f"Missing roots: {missing_roots}")
    print(f"TIFF files scanned: {scanned_tiffs}")
    print(f"Existing paths skipped: {skipped_existing}")
    print(f"Duplicate filenames found during scan: {duplicate_scan_filenames}")
    print(f"New files discovered: {len(discovered_by_path)}")
    print(f"Features inserted: {inserted}")
    print(f"Skipped unknown client in path: {skipped_unknown_client}")
    print(f"Skipped invalid filename: {skipped_invalid_name}")
    print(f"Skipped because no valid datum transformation was available: {skipped_missing_transformation}")
    print(f"Skipped invalid/error: {skipped_invalid}")
    print(f"Production features inserted: {production_inserted}")
    print(f"Production existing paths skipped: {production_skipped_existing}")
    print(f"Production rows skipped due to lock: {production_skipped_locked}")
    print(f"Production rows skipped due to errors: {production_skipped_error}")
    print(f"CSV output: {new_files_csv}")
    elapsed_seconds = time.perf_counter() - started_at
    run_report.record("run", "completed" if run_report.issue_count == 0 else "completed_with_issues",
                      output_path=new_files_csv,
                      message=(f"scanned={scanned_tiffs}; staging_inserted={inserted}; "
                               f"production_inserted={production_inserted}; "
                               f"production_locked={production_skipped_locked}; "
                               f"production_errors={production_skipped_error}; "
                               f"diagnostic_issues={run_report.issue_count}; elapsed_seconds={elapsed_seconds:.2f}"))
    print(f"Detailed diagnostics: {run_report.path}")
    print(f"Elapsed seconds: {elapsed_seconds:.2f}; report issues: {run_report.issue_count}")
    final_exit_code = 1 if run_report.issue_count else 0
    run_report.close()
    sys.exit(final_exit_code)
