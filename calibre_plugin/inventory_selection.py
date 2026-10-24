"""Selection state and action target helpers for the inventory dialog."""


def sync_checks_to_rows(dialog):
    for row, uploads, metadata, read in zip(
            dialog.rows, dialog.checks, dialog.meta_checks, dialog.read_checks):
        row["upload"] = bool(uploads.isChecked())
        row["metadata"] = bool(metadata.isChecked())
        row["read"] = bool(read.isChecked())


def set_checked_quietly(check_widget, value):
    """Set one checkbox without triggering summary recalculations."""
    blocker = getattr(check_widget, "blockSignals", None)
    if blocker is not None:
        try:
            blocker(True)
        except Exception:
            blocker = None
    try:
        check_widget.setChecked(value)
    except Exception:
        pass
    if blocker is not None:
        try:
            blocker(False)
        except Exception:
            pass


def set_selection(dialog, chooser, clear_deletions=False):
    """Apply recommended or cleared choices to rows and checkbox widgets."""
    sync_checks_to_rows(dialog)
    if clear_deletions:
        for row, uploads, metadata, read in zip(
                dialog.rows, dialog.checks, dialog.meta_checks,
                dialog.read_checks):
            row.pop("delete_cloud", None)
            row["upload"] = False
            row["metadata"] = False
            row["read"] = bool(row.get("read_in_cloud"))
            for check, value in ((uploads, False), (metadata, False),
                                 (read, row["read"])):
                dialog._set_checked_quietly(check, value)
        if getattr(dialog, "table", None) is not None:
            dialog._rebuild_table(dialog.rows)
        else:
            dialog._update_summary()
        return
    for row, uploads, metadata, read in zip(
            dialog.rows, dialog.checks, dialog.meta_checks,
            dialog.read_checks):
        if row.get("delete_cloud"):
            continue
        wanted = tuple(chooser(row))
        upload, meta = bool(wanted[0]), bool(wanted[1])
        mark_read = bool(row.get("read_in_cloud"))
        row["upload"], row["metadata"], row["read"] = upload, meta, mark_read
        for check, value in ((uploads, upload), (metadata, meta),
                             (read, mark_read)):
            dialog._set_checked_quietly(check, value)
    dialog._update_summary()


def selected_upload_ids(dialog, selector):
    return selector([
        dict(row, selected=check.isChecked())
        for row, check in zip(dialog.rows, dialog.checks)
    ])


def selected_metadata_ids(dialog, selector):
    return selector([
        dict(row, selected=check.isChecked())
        for row, check in zip(dialog.rows, dialog.meta_checks)
    ])


def selected_read_targets(dialog):
    return [str(row["tolino_id"]) for row, check
            in zip(dialog.rows, dialog.read_checks)
            if check.isChecked() and row.get("tolino_id")
            and not row.get("delete_cloud")
            and not row.get("read_in_cloud")]


def selected_read_unmarks(dialog):
    return [str(row["tolino_id"]) for row, check
            in zip(dialog.rows, dialog.read_checks)
            if not check.isChecked() and row.get("tolino_id")
            and not row.get("delete_cloud")
            and row.get("read_in_cloud")]


def selected_cloud_deletions(dialog):
    return [(str(row.get("uuid") or ""), str(row["tolino_id"]),
             str(row.get("title") or ""))
            for row in dialog.rows
            if row.get("delete_cloud") and row.get("tolino_id")]


def marked_row_indexes(dialog, selected_rows, message_box):
    """Get selected visible rows, displaying the existing empty-selection hint."""
    marked = selected_rows(dialog.table)
    if not marked:
        message_box.information(
            dialog, "Aktionen für markierte Bücher",
            "Markiere zuerst mindestens ein Buch in der Tabelle. "
            "Mehrere Bücher wählst du mit Strg-/Shift-Klick aus.")
    return marked


def column_applies(key, row):
    if row.get("delete_cloud"):
        return False
    if key == "upload":
        return row.get("book_id") is not None
    if key == "metadata":
        return row.get("book_id") is not None and bool(row.get("tolino_id"))
    if key == "read":
        return bool(row.get("tolino_id"))
    return False


def set_row_choice(dialog, index, key, value):
    """Set a single row and widget; False means it already had that value."""
    row = dialog.rows[index]
    if row.get("delete_cloud"):
        return False
    if key == "read":
        check = dialog.read_checks[index]
        if row.get("read_known") is False:
            return False
        if value and row.get("read_in_cloud"):
            return False
        if not value and not row.get("read_in_cloud"):
            return False
        if bool(row.get("read")) == value and bool(check.isChecked()) == value:
            return False
        row["read"] = value
    else:
        check = (dialog.checks if key == "upload"
                 else dialog.meta_checks)[index]
        if bool(row.get(key)) == value and bool(check.isChecked()) == value:
            return False
        row[key] = value
    dialog._set_checked_quietly(check, value)
    return True
