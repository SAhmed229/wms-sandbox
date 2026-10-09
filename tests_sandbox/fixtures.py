"""Small warehouses whose quantities and operating constraints are explicit."""


def warehouse():
    return {
        "schema_version": "1.0",
        "facility": {"name": "Test warehouse", "start_time": "2026-10-08T08:00:00-04:00"},
        "locations": [
            {"id": "store-a", "x": 120, "y": 0, "kind": "storage", "capacity_pallets": 5},
            {"id": "store-b", "x": 100, "y": 20, "kind": "storage", "capacity_pallets": 5},
            {"id": "stage", "x": 10, "y": 0, "kind": "staging", "capacity_pallets": 1},
            {"id": "dock", "x": 0, "y": 0, "kind": "dock", "capacity_pallets": 1},
        ],
        "docks": [{"id": "D1", "location_id": "dock", "staging_location_id": "stage"}],
        "forklifts": [{"id": "F1", "start_location_id": "store-a"}],
        "inventory": [
            {"pallet_id": "P1", "sku_id": "shared-sku", "owner_id": "tenant-a", "location_id": "store-a", "quantity": 12},
            {"pallet_id": "P2", "sku_id": "shared-sku", "owner_id": "tenant-b", "location_id": "store-b", "quantity": 9},
        ],
        "orders": [
            {"order_id": "O1", "owner_id": "tenant-a", "release_seconds": 0, "arrival_seconds": 1000,
             "deadline_seconds": 1500, "dock_id": "D1", "lines": [{"pallet_id": "P1", "quantity": 5}]},
            {"order_id": "O2", "owner_id": "tenant-b", "release_seconds": 0, "arrival_seconds": 1000,
             "deadline_seconds": 1500, "dock_id": "D1", "lines": [{"pallet_id": "P2", "quantity": 9}]},
            {"order_id": "O3", "owner_id": "tenant-a", "release_seconds": 1500, "arrival_seconds": 1700,
             "deadline_seconds": 2100, "dock_id": "D1", "lines": [{"pallet_id": "P1", "quantity": 4}]},
        ],
        "historical_moves": [],
    }


def csv_files():
    return {
        "locations.csv": "id,x,y,kind,capacity_pallets\nS,100,0,storage,2\nT,0,10,staging,1\nD,0,0,dock,1\n",
        "docks.csv": "id,location_id,staging_location_id\nD1,D,T\n",
        "forklifts.csv": "id,start_location_id\nF1,S\n",
        "inventory.csv": "pallet_id,sku_id,owner_id,location_id,quantity\nP1,X,A,S,8\nP2,X,A,S,4\n",
        "orders.csv": "order_id,owner_id,release_seconds,arrival_seconds,deadline_seconds,dock_id,pallet_id,quantity,observed_departure_seconds\nO1,A,0,400,700,D1,P1,3,560\nO1,A,0,400,700,D1,P2,4,560\n",
    }
