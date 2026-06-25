# Shipment & Pallet Tracking System

A local Streamlit application for recording box shipments to stores, tracking
couriers, pallets, and deliveries, and producing store/group/pallet reports.

## Features

- Fast group-based shipment entry
- Individual store-level shipment detail records
- Automatic pallet IDs (`PAL-YYYYMMDD-001`)
- Dashboard metrics and weekly/monthly charts
- Searchable and filterable shipment history
- Shipment editing and deletion
- Store and group reporting
- Pallet lookup
- Excel and CSV exports
- Group/store administration
- Persistent audit trail

## Run

```powershell
pip install -r requirements.txt
streamlit run app.py
```

The SQLite database (`shipment_tracking.db`) is created automatically beside
the application files. Default example groups and stores are seeded on first
run.

## Backup

Close the application or ensure no shipment is being saved, then copy
`shipment_tracking.db` to your backup location.
