-- Warehouse Pre-Positioning Optimizer: Database Initialization
-- Creates and seeds tables for the WMS adapter
--
-- ══════════════════════════════════════════════════════════════════════════════
-- COORDINATE SYSTEM
-- ══════════════════════════════════════════════════════════════════════════════
-- Origin (0, 0): south-west corner of the building (outside dock apron).
-- X-axis: east (metres). Building width: 120 m.
-- Y-axis: north (metres). Building depth: 80 m.
-- Dock wall: y = 0 (south face). Four dock doors at y = 0.
--
-- Layout (bird's-eye):
--
--  y=80 ┌──────────────────────────────────────────────────────────────┐
--       │  COLD ZONE  (aisles 10-11, y=71-77)                         │
--       │  Zone C — slow movers (aisles 7-9,  y=53-65)                │
--       │  Zone B — med velocity (aisles 4-6,  y=31-47)               │
--       │  Zone A — fast movers  (aisles 1-3,  y=10-26)               │
--  y=3  │  ── Staging lanes ─────────────────────────────────────────  │
--  y=0  └──[D1]────────[D2]────────[D3]────────[D4]───────────────────┘
--          x=10        x=40        x=70        x=100
--
-- Dock door coordinates:
--   Door 1: (10.0,  0.0)   Dock 1 left side of building
--   Door 2: (40.0,  0.0)   Dock 2 left-centre
--   Door 3: (70.0,  0.0)   Dock 3 right-centre
--   Door 4: (100.0, 0.0)   Dock 4 right side
--
-- Aisle spacing: 8 m between aisle centre lines (y-axis).
-- Bay spacing:   15 m between bay centres within each aisle (x-axis).
-- These spacings produce T_saved values of 5–60 s per move depending on
-- distance from dock, which is realistic for a 120×80 m distribution centre.
--
-- To replace with real facility coordinates:
--   1. Export x/y from your CAD/BIM or WMS floor plan (confirm metres vs feet).
--   2. UPDATE locations SET x=<real_x>, y=<real_y> WHERE location_id=...;
--   3. Update dock door coords in config.yml (optimization.dock_door_coordinates)
--      or in MovementScorer(dock_door_coords={1:(10.0,0.0), ...}).
-- ══════════════════════════════════════════════════════════════════════════════

-- ──────────────────────────────────────────────────────────────────────────────
-- LOCATIONS
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS locations (
    location_id        VARCHAR(50) PRIMARY KEY,
    zone               VARCHAR(20) NOT NULL,
    aisle              INTEGER     NOT NULL,
    bay                INTEGER     NOT NULL,
    level              INTEGER     NOT NULL DEFAULT 0,
    x                  NUMERIC(8,2) NOT NULL,
    y                  NUMERIC(8,2) NOT NULL,
    temperature_zone   VARCHAR(20) NOT NULL DEFAULT 'AMBIENT',
    max_weight_kg      NUMERIC(8,2) NOT NULL DEFAULT 2000.0,
    max_volume_m3      NUMERIC(8,2) NOT NULL DEFAULT 10.0,
    is_staging         BOOLEAN     NOT NULL DEFAULT FALSE,
    nearest_dock_door  INTEGER
);

-- ── Staging lanes (y ≈ 3 m, immediately behind dock apron) ────────────────────
-- Two staging slots per dock door. Slots are ±5 m either side of the door centre.
-- Oversized bays (2500 kg / 12 m³) to hold full pallet loads pre-staged for loading.
INSERT INTO locations (location_id, zone, aisle, bay, level, x, y, temperature_zone, max_weight_kg, max_volume_m3, is_staging, nearest_dock_door) VALUES
('STAGE-D1-A', 'STAGING',  1, 1, 0,   5.0,  3.0, 'AMBIENT', 2500, 12, TRUE, 1),
('STAGE-D1-B', 'STAGING',  1, 2, 0,  15.0,  3.0, 'AMBIENT', 2500, 12, TRUE, 1),
('STAGE-D2-A', 'STAGING',  2, 1, 0,  35.0,  3.0, 'AMBIENT', 2500, 12, TRUE, 2),
('STAGE-D2-B', 'STAGING',  2, 2, 0,  45.0,  3.0, 'AMBIENT', 2500, 12, TRUE, 2),
('STAGE-D3-A', 'STAGING',  3, 1, 0,  65.0,  3.0, 'AMBIENT', 2500, 12, TRUE, 3),
('STAGE-D3-B', 'STAGING',  3, 2, 0,  75.0,  3.0, 'AMBIENT', 2500, 12, TRUE, 3),
('STAGE-D4-A', 'STAGING',  4, 1, 0,  95.0,  3.0, 'AMBIENT', 2500, 12, TRUE, 4),
('STAGE-D4-B', 'STAGING',  4, 2, 0, 105.0,  3.0, 'AMBIENT', 2500, 12, TRUE, 4),
-- Cold staging slots (temperature-controlled, serve dock 3 which handles cold carriers)
('STAGE-COLD-A', 'STAGING', 3, 3, 0, 62.0, 3.0, 'CHILLED', 2000, 10, TRUE, 3),
('STAGE-COLD-B', 'STAGING', 3, 4, 0, 78.0, 3.0, 'FROZEN',  1800,  9, TRUE, 3);

-- ── Zone A — fast movers (aisles 1-3, y = 10 / 18 / 26) ──────────────────────
-- Closest bulk zone to the dock wall. ABC-A SKUs stored here.
-- 8 bays per aisle × 3 aisles = 24 locations.
-- Bay x positions: 10, 25, 40, 55, 70, 85, 100, 110
INSERT INTO locations (location_id, zone, aisle, bay, level, x, y, temperature_zone, max_weight_kg, max_volume_m3, is_staging, nearest_dock_door) VALUES
-- Aisle 1 (y=10) — closest to dock, serves all four doors
('LOC-A101', 'A', 1, 1, 0,  10.0, 10.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A102', 'A', 1, 2, 0,  25.0, 10.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A103', 'A', 1, 3, 0,  40.0, 10.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A104', 'A', 1, 4, 0,  55.0, 10.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A105', 'A', 1, 5, 0,  70.0, 10.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A106', 'A', 1, 6, 0,  85.0, 10.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A107', 'A', 1, 7, 0, 100.0, 10.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A108', 'A', 1, 8, 0, 110.0, 10.0, 'AMBIENT', 2000, 10, FALSE, NULL),
-- Aisle 2 (y=18)
('LOC-A201', 'A', 2, 1, 0,  10.0, 18.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A202', 'A', 2, 2, 0,  25.0, 18.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A203', 'A', 2, 3, 0,  40.0, 18.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A204', 'A', 2, 4, 0,  55.0, 18.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A205', 'A', 2, 5, 0,  70.0, 18.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A206', 'A', 2, 6, 0,  85.0, 18.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A207', 'A', 2, 7, 0, 100.0, 18.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A208', 'A', 2, 8, 0, 110.0, 18.0, 'AMBIENT', 2000, 10, FALSE, NULL),
-- Aisle 3 (y=26)
('LOC-A301', 'A', 3, 1, 0,  10.0, 26.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A302', 'A', 3, 2, 0,  25.0, 26.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A303', 'A', 3, 3, 0,  40.0, 26.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A304', 'A', 3, 4, 0,  55.0, 26.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A305', 'A', 3, 5, 0,  70.0, 26.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A306', 'A', 3, 6, 0,  85.0, 26.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A307', 'A', 3, 7, 0, 100.0, 26.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-A308', 'A', 3, 8, 0, 110.0, 26.0, 'AMBIENT', 2000, 10, FALSE, NULL);

-- ── Zone B — medium velocity (aisles 4-6, y = 34 / 42 / 50) ─────────────────
-- 6 bays per aisle × 3 aisles = 18 locations.
-- Bay x positions: 10, 30, 50, 70, 90, 110
INSERT INTO locations (location_id, zone, aisle, bay, level, x, y, temperature_zone, max_weight_kg, max_volume_m3, is_staging, nearest_dock_door) VALUES
-- Aisle 4 (y=34)
('LOC-B401', 'B', 4, 1, 0,  10.0, 34.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-B402', 'B', 4, 2, 0,  30.0, 34.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-B403', 'B', 4, 3, 0,  50.0, 34.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-B404', 'B', 4, 4, 0,  70.0, 34.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-B405', 'B', 4, 5, 0,  90.0, 34.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-B406', 'B', 4, 6, 0, 110.0, 34.0, 'AMBIENT', 2000, 10, FALSE, NULL),
-- Aisle 5 (y=42)
('LOC-B501', 'B', 5, 1, 0,  10.0, 42.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-B502', 'B', 5, 2, 0,  30.0, 42.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-B503', 'B', 5, 3, 0,  50.0, 42.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-B504', 'B', 5, 4, 0,  70.0, 42.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-B505', 'B', 5, 5, 0,  90.0, 42.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-B506', 'B', 5, 6, 0, 110.0, 42.0, 'AMBIENT', 2000, 10, FALSE, NULL),
-- Aisle 6 (y=50)
('LOC-B601', 'B', 6, 1, 0,  10.0, 50.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-B602', 'B', 6, 2, 0,  30.0, 50.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-B603', 'B', 6, 3, 0,  50.0, 50.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-B604', 'B', 6, 4, 0,  70.0, 50.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-B605', 'B', 6, 5, 0,  90.0, 50.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-B606', 'B', 6, 6, 0, 110.0, 50.0, 'AMBIENT', 2000, 10, FALSE, NULL);

-- ── Zone C — slow movers (aisles 7-9, y = 58 / 65 / 72) ─────────────────────
-- 4 bays per aisle × 3 aisles = 12 locations.
-- Bay x positions: 15, 45, 75, 105
INSERT INTO locations (location_id, zone, aisle, bay, level, x, y, temperature_zone, max_weight_kg, max_volume_m3, is_staging, nearest_dock_door) VALUES
-- Aisle 7 (y=58)
('LOC-C701', 'C', 7, 1, 0,  15.0, 58.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-C702', 'C', 7, 2, 0,  45.0, 58.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-C703', 'C', 7, 3, 0,  75.0, 58.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-C704', 'C', 7, 4, 0, 105.0, 58.0, 'AMBIENT', 2000, 10, FALSE, NULL),
-- Aisle 8 (y=65)
('LOC-C801', 'C', 8, 1, 0,  15.0, 65.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-C802', 'C', 8, 2, 0,  45.0, 65.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-C803', 'C', 8, 3, 0,  75.0, 65.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-C804', 'C', 8, 4, 0, 105.0, 65.0, 'AMBIENT', 2000, 10, FALSE, NULL),
-- Aisle 9 (y=72) — hazmat segregation bay at far end
('LOC-C901', 'C', 9, 1, 0,  15.0, 72.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-C902', 'C', 9, 2, 0,  45.0, 72.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-C903', 'C', 9, 3, 0,  75.0, 72.0, 'AMBIENT', 2000, 10, FALSE, NULL),
('LOC-C904', 'C', 9, 4, 0, 105.0, 72.0, 'AMBIENT', 2000, 10, FALSE, NULL);

-- ── Cold zone (aisles 10-11, y = 76) ─────────────────────────────────────────
-- Frozen and chilled bays in an insulated room at the north end.
-- Bay x positions: 10, 30, 50, 70 (frozen); 80, 95, 110 (chilled — separate rooms)
INSERT INTO locations (location_id, zone, aisle, bay, level, x, y, temperature_zone, max_weight_kg, max_volume_m3, is_staging, nearest_dock_door) VALUES
-- Frozen (y=76, west side)
('LOC-FROZ01', 'COLD', 10, 1, 0,  10.0, 76.0, 'FROZEN',  1500, 8, FALSE, NULL),
('LOC-FROZ02', 'COLD', 10, 2, 0,  25.0, 76.0, 'FROZEN',  1500, 8, FALSE, NULL),
('LOC-FROZ03', 'COLD', 10, 3, 0,  40.0, 76.0, 'FROZEN',  1500, 8, FALSE, NULL),
('LOC-FROZ04', 'COLD', 10, 4, 0,  55.0, 76.0, 'FROZEN',  1500, 8, FALSE, NULL),
-- Chilled (y=76, east side — physically separated from frozen)
('LOC-CHIL01', 'COLD', 11, 1, 0,  75.0, 76.0, 'CHILLED', 1800, 9, FALSE, NULL),
('LOC-CHIL02', 'COLD', 11, 2, 0,  90.0, 76.0, 'CHILLED', 1800, 9, FALSE, NULL),
('LOC-CHIL03', 'COLD', 11, 3, 0, 105.0, 76.0, 'CHILLED', 1800, 9, FALSE, NULL),
('LOC-CHIL04', 'COLD', 11, 4, 0, 115.0, 76.0, 'CHILLED', 1800, 9, FALSE, NULL);

-- ──────────────────────────────────────────────────────────────────────────────
-- DOCK DOOR REFERENCE TABLE (optional — used for reporting and graph building)
-- ──────────────────────────────────────────────────────────────────────────────
-- This table is NOT read by the WMS adapter (which derives door coords from the
-- locations table). It is here for human reference and for feeding into the
-- WarehouseGraph / MovementScorer at startup.
--
-- Usage in Python (copy into src/api/main.py lifespan):
--   from src.scoring.value_function import _DEFAULT_DOCK_DOOR_COORDS
--   _DEFAULT_DOCK_DOOR_COORDS.update({1: (10.0, 0.0), 2: (40.0, 0.0),
--                                      3: (70.0, 0.0), 4: (100.0, 0.0)})
CREATE TABLE IF NOT EXISTS dock_doors (
    dock_door  INTEGER PRIMARY KEY,
    x          NUMERIC(8,2) NOT NULL,
    y          NUMERIC(8,2) NOT NULL,
    description VARCHAR(100)
);

INSERT INTO dock_doors (dock_door, x, y, description) VALUES
(1,  10.0, 0.0, 'West dock — ambient, high-velocity outbound'),
(2,  40.0, 0.0, 'Left-centre dock — ambient general outbound'),
(3,  70.0, 0.0, 'Right-centre dock — cold chain carrier lane'),
(4, 100.0, 0.0, 'East dock — ambient, hazmat-approved carrier lane');

-- ──────────────────────────────────────────────────────────────────────────────
-- SKUS
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS skus (
    sku_id                    VARCHAR(50) PRIMARY KEY,
    description               TEXT        NOT NULL,
    weight_kg                 NUMERIC(8,3) NOT NULL,
    volume_m3                 NUMERIC(8,4) NOT NULL,
    hazmat_class              VARCHAR(10),
    requires_temperature_zone VARCHAR(20) NOT NULL DEFAULT 'AMBIENT',
    abc_class                 CHAR(1)     NOT NULL DEFAULT 'C'
);

-- Seed: 50 SKUs (mix of ABC, temperatures, some hazmat)
INSERT INTO skus (sku_id, description, weight_kg, volume_m3, hazmat_class, requires_temperature_zone, abc_class) VALUES
-- Class A ambient SKUs (high velocity)
('SKU-A001', 'Widget Type A - High Velocity',       45.0,  0.250, NULL, 'AMBIENT', 'A'),
('SKU-A002', 'Component B Assembly',               120.0,  0.600, NULL, 'AMBIENT', 'A'),
('SKU-A003', 'Packaging Material Roll',             15.0,  0.800, NULL, 'AMBIENT', 'A'),
('SKU-A004', 'Electronic Module X200',               8.5,  0.040, NULL, 'AMBIENT', 'A'),
('SKU-A005', 'Consumer Goods Unit P',               30.0,  0.200, NULL, 'AMBIENT', 'A'),
('SKU-A006', 'Retail Box Assembly RB',              22.0,  0.180, NULL, 'AMBIENT', 'A'),
('SKU-A007', 'Bulk Powder Container',              200.0,  1.000, NULL, 'AMBIENT', 'A'),
('SKU-A008', 'Automotive Part AF-12',               85.0,  0.400, NULL, 'AMBIENT', 'A'),
('SKU-A009', 'Standard Pallet Load SL',            500.0,  2.000, NULL, 'AMBIENT', 'A'),
('SKU-A010', 'Fast-Moving Consumer Good',           18.0,  0.120, NULL, 'AMBIENT', 'A'),
-- Class B ambient SKUs (medium velocity)
('SKU-B001', 'Medium Velocity Widget',              60.0,  0.350, NULL, 'AMBIENT', 'B'),
('SKU-B002', 'Industrial Component IC-7',          150.0,  0.750, NULL, 'AMBIENT', 'B'),
('SKU-B003', 'Spare Part Assembly SP-23',           35.0,  0.220, NULL, 'AMBIENT', 'B'),
('SKU-B004', 'Tool Kit TK-Standard',               12.0,  0.060, NULL, 'AMBIENT', 'B'),
('SKU-B005', 'Office Supply Bundle OS',              5.0,  0.030, NULL, 'AMBIENT', 'B'),
('SKU-B006', 'Machine Sub-Assembly MSA',           300.0,  1.500, NULL, 'AMBIENT', 'B'),
('SKU-B007', 'Textile Roll TR-100',                 80.0,  0.900, NULL, 'AMBIENT', 'B'),
('SKU-B008', 'Plastic Component PC-45',             25.0,  0.150, NULL, 'AMBIENT', 'B'),
('SKU-B009', 'Metal Bracket MB-12',                 40.0,  0.200, NULL, 'AMBIENT', 'B'),
('SKU-B010', 'Rubber Seal RS-8',                     2.0,  0.010, NULL, 'AMBIENT', 'B'),
-- Class C ambient SKUs (low velocity)
('SKU-C001', 'Slow Mover SC-001',                   70.0,  0.400, NULL, 'AMBIENT', 'C'),
('SKU-C002', 'Archived Document Box',               10.0,  0.500, NULL, 'AMBIENT', 'C'),
('SKU-C003', 'Legacy Part LP-99',                  180.0,  0.900, NULL, 'AMBIENT', 'C'),
('SKU-C004', 'Surplus Material SM-7',               55.0,  0.300, NULL, 'AMBIENT', 'C'),
('SKU-C005', 'Overflow Stock OV-3',                 90.0,  0.450, NULL, 'AMBIENT', 'C'),
-- Frozen SKUs
('SKU-F001', 'Frozen Meal Tray FM-1',                8.0,  0.040, NULL, 'FROZEN',  'A'),
('SKU-F002', 'Ice Cream Carton IC-2L',               2.5,  0.002, NULL, 'FROZEN',  'A'),
('SKU-F003', 'Frozen Vegetable Pack FV',             1.0,  0.001, NULL, 'FROZEN',  'B'),
('SKU-F004', 'Frozen Protein Portion PP',            5.0,  0.005, NULL, 'FROZEN',  'B'),
('SKU-F005', 'Bulk Frozen Commodity BF',            50.0,  0.050, NULL, 'FROZEN',  'C'),
-- Chilled SKUs
('SKU-CH001', 'Fresh Dairy Product DP',              3.0,  0.003, NULL, 'CHILLED', 'A'),
('SKU-CH002', 'Chilled Juice Carton CJ',             1.5,  0.001, NULL, 'CHILLED', 'A'),
('SKU-CH003', 'Yogurt Multipack YM',                 0.8,  0.001, NULL, 'CHILLED', 'B'),
('SKU-CH004', 'Prepared Meal Kit PMK',               4.5,  0.004, NULL, 'CHILLED', 'B'),
('SKU-CH005', 'Cold Cut Deli Pack CD',               2.0,  0.002, NULL, 'CHILLED', 'C'),
-- Hazmat SKUs (stored in Zone C east bays, served by dock 4)
('SKU-HZ001', 'Industrial Solvent IS-1',            30.0,  0.025, '3',   'AMBIENT', 'B'),
('SKU-HZ002', 'Cleaning Chemical CC-8',             25.0,  0.020, '8',   'AMBIENT', 'C'),
('SKU-HZ003', 'Compressed Gas Cylinder CG',         50.0,  0.100, '2',   'AMBIENT', 'C'),
('SKU-HZ004', 'Battery Pack BP-LiIon',              15.0,  0.010, '9',   'AMBIENT', 'B'),
('SKU-HZ005', 'Oxidizing Agent OA-15',              20.0,  0.018, '5.1', 'AMBIENT', 'C'),
-- Additional Class A ambient SKUs
('SKU-A011', 'Fast Mover Product FM11',             40.0,  0.210, NULL, 'AMBIENT', 'A'),
('SKU-A012', 'Priority SKU PR-12',                  55.0,  0.290, NULL, 'AMBIENT', 'A'),
('SKU-A013', 'High Demand Item HD-13',              72.0,  0.380, NULL, 'AMBIENT', 'A'),
('SKU-A014', 'Velocity Leader VL-14',              110.0,  0.550, NULL, 'AMBIENT', 'A'),
('SKU-A015', 'Top Seller TS-15',                    28.0,  0.160, NULL, 'AMBIENT', 'A'),
('SKU-B011', 'Medium Mover MM-11',                  65.0,  0.340, NULL, 'AMBIENT', 'B'),
('SKU-B012', 'Standard Volume SV-12',              105.0,  0.520, NULL, 'AMBIENT', 'B'),
('SKU-B013', 'Regular Product RP-13',               42.0,  0.230, NULL, 'AMBIENT', 'B'),
('SKU-B014', 'Planned Order PO-14',                 88.0,  0.430, NULL, 'AMBIENT', 'B'),
('SKU-B015', 'Seasonal Item SI-15',                 35.0,  0.190, NULL, 'AMBIENT', 'B');

-- ──────────────────────────────────────────────────────────────────────────────
-- INVENTORY POSITIONS
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS inventory_positions (
    position_id  VARCHAR(50) PRIMARY KEY,
    sku_id       VARCHAR(50) NOT NULL REFERENCES skus(sku_id),
    location_id  VARCHAR(50) NOT NULL REFERENCES locations(location_id),
    quantity     INTEGER     NOT NULL DEFAULT 0,
    lot_number   VARCHAR(50),
    expiry_date  TIMESTAMPTZ
);

-- Seed: ABC-A SKUs placed in Zone A (close to dock, short T_saved potential);
--       ABC-B in Zone B (mid-building, moderate T_saved);
--       ABC-C and hazmat in Zone C (far from dock, high T_saved if pre-staged).
--       Cold SKUs in the cold zone.
--
-- T_saved examples at forklift_speed = 2.2 m/s:
--   SKU-A001 at LOC-A101 (10,10) → staging STAGE-D1-A (5,3):
--     dist_from = |10-10|+|10-0| = 10 m  →  4.5 s to door 1
--     dist_to   = |5-10| +|3-0| = 8 m   →  3.6 s to door 1
--     T_saved = (10-8)/2.2 = 0.9 s  (already near dock)
--
--   SKU-C001 at LOC-C701 (15,58) → staging STAGE-D2-A (35,3):
--     dist_from = |15-40|+|58-0| = 83 m → 37.7 s to door 2
--     dist_to   = |35-40|+|3-0|  =  8 m →  3.6 s to door 2
--     T_saved = (83-8)/2.2 = 34.1 s  (significant saving)
--
--   This spread gives a ~40× range of T_saved values across the seed data,
--   exercising the full range of the scoring function.
INSERT INTO inventory_positions (position_id, sku_id, location_id, quantity, lot_number, expiry_date) VALUES
-- Zone A — high velocity (aisle 1, closest row)
('INV-001', 'SKU-A001', 'LOC-A101',  50, 'LOT-2024-001', NULL),
('INV-002', 'SKU-A002', 'LOC-A102',  20, 'LOT-2024-002', NULL),
('INV-003', 'SKU-A003', 'LOC-A103', 100, 'LOT-2024-003', NULL),
('INV-004', 'SKU-A004', 'LOC-A104', 200, 'LOT-2024-004', NULL),
('INV-005', 'SKU-A005', 'LOC-A105',  75, 'LOT-2024-005', NULL),
('INV-006', 'SKU-A006', 'LOC-A106',  60, 'LOT-2024-006', NULL),
('INV-007', 'SKU-A007', 'LOC-A107',  15, 'LOT-2024-007', NULL),
('INV-008', 'SKU-A008', 'LOC-A108',  30, 'LOT-2024-008', NULL),
-- Zone A — aisle 2
('INV-009', 'SKU-A009', 'LOC-A201',  10, 'LOT-2024-009', NULL),
('INV-010', 'SKU-A010', 'LOC-A202', 120, 'LOT-2024-010', NULL),
('INV-034', 'SKU-A011', 'LOC-A203',  35, 'LOT-2024-034', NULL),
('INV-035', 'SKU-A012', 'LOC-A204',  22, 'LOT-2024-035', NULL),
('INV-036', 'SKU-A013', 'LOC-A205',  18, 'LOT-2024-036', NULL),
('INV-037', 'SKU-A014', 'LOC-A206',  11, 'LOT-2024-037', NULL),
('INV-038', 'SKU-A015', 'LOC-A207',  60, 'LOT-2024-038', NULL),
-- Zone B — medium velocity (aisle 4, mid-building)
('INV-011', 'SKU-B001', 'LOC-B401',  40, 'LOT-2024-011', NULL),
('INV-012', 'SKU-B002', 'LOC-B402',  12, 'LOT-2024-012', NULL),
('INV-013', 'SKU-B003', 'LOC-B403',  55, 'LOT-2024-013', NULL),
('INV-014', 'SKU-B004', 'LOC-B404',  80, 'LOT-2024-014', NULL),
('INV-015', 'SKU-B005', 'LOC-B405', 300, 'LOT-2024-015', NULL),
('INV-016', 'SKU-B006', 'LOC-B406',   8, 'LOT-2024-016', NULL),
-- Zone B — aisle 5
('INV-017', 'SKU-B007', 'LOC-B501',   5, 'LOT-2024-017', NULL),
('INV-018', 'SKU-B008', 'LOC-B502',  90, 'LOT-2024-018', NULL),
('INV-019', 'SKU-B009', 'LOC-B503',  45, 'LOT-2024-019', NULL),
('INV-020', 'SKU-B010', 'LOC-B504', 500, 'LOT-2024-020', NULL),
('INV-039', 'SKU-B011', 'LOC-B505',  28, 'LOT-2024-039', NULL),
('INV-040', 'SKU-B012', 'LOC-B506',  16, 'LOT-2024-040', NULL),
-- Zone B — aisle 6 (furthest B-zone row)
('INV-041', 'SKU-B013', 'LOC-B601',  33, 'LOT-2024-041', NULL),
('INV-042', 'SKU-B014', 'LOC-B602',  19, 'LOT-2024-042', NULL),
('INV-043', 'SKU-B015', 'LOC-B603',  44, 'LOT-2024-043', NULL),
-- Zone C — slow movers (high T_saved when pre-staged)
('INV-021', 'SKU-C001', 'LOC-C701',  25, 'LOT-2024-021', NULL),
('INV-022', 'SKU-C002', 'LOC-C702',  18, 'LOT-2024-022', NULL),
('INV-023', 'SKU-C003', 'LOC-C703',   7, 'LOT-2024-023', NULL),
('INV-024', 'SKU-C004', 'LOC-C801',  33, 'LOT-2024-024', NULL),
('INV-025', 'SKU-C005', 'LOC-C802',  14, 'LOT-2024-025', NULL),
-- Hazmat — Zone C east bays, served by dock 4 (hazmat-approved)
('INV-031', 'SKU-HZ001', 'LOC-C703',  20, 'LOT-HAZ-001', NULL),
('INV-032', 'SKU-HZ002', 'LOC-C803',  15, 'LOT-HAZ-002', NULL),
('INV-033', 'SKU-HZ003', 'LOC-C903',  10, 'LOT-HAZ-003', NULL),
('INV-044', 'SKU-HZ004', 'LOC-C704',  12, 'LOT-HAZ-004', NULL),
('INV-045', 'SKU-HZ005', 'LOC-C904',   8, 'LOT-HAZ-005', NULL),
-- Cold storage — frozen
('INV-026', 'SKU-F001', 'LOC-FROZ01', 200, 'LOT-FROZEN-001', (NOW() + INTERVAL  '90 days')),
('INV-027', 'SKU-F002', 'LOC-FROZ02', 150, 'LOT-FROZEN-002', (NOW() + INTERVAL  '60 days')),
('INV-028', 'SKU-F003', 'LOC-FROZ03', 300, 'LOT-FROZEN-003', (NOW() + INTERVAL  '45 days')),
('INV-046', 'SKU-F004', 'LOC-FROZ04',  80, 'LOT-FROZEN-004', (NOW() + INTERVAL  '30 days')),
-- Cold storage — chilled
('INV-029', 'SKU-CH001', 'LOC-CHIL01', 100, 'LOT-CHILL-001', (NOW() + INTERVAL '14 days')),
('INV-030', 'SKU-CH002', 'LOC-CHIL02',  80, 'LOT-CHILL-002', (NOW() + INTERVAL '10 days')),
('INV-047', 'SKU-CH003', 'LOC-CHIL03',  60, 'LOT-CHILL-003', (NOW() + INTERVAL  '7 days')),
('INV-048', 'SKU-CH004', 'LOC-CHIL04',  40, 'LOT-CHILL-004', (NOW() + INTERVAL  '5 days'));

-- ──────────────────────────────────────────────────────────────────────────────
-- CARRIER APPOINTMENTS
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS carrier_appointments (
    appointment_id      VARCHAR(50) PRIMARY KEY,
    carrier             VARCHAR(100) NOT NULL,
    dock_door           INTEGER      NOT NULL,
    scheduled_arrival   TIMESTAMPTZ  NOT NULL,
    scheduled_departure TIMESTAMPTZ  NOT NULL,
    status              VARCHAR(20)  NOT NULL DEFAULT 'SCHEDULED'
);

-- Seed: 4 appointments spread across the shift, one per dock door.
-- Door 1 (x=10): ambient west zone.  Door 2 (x=40): ambient centre.
-- Door 3 (x=70): cold-chain carrier. Door 4 (x=100): ambient/hazmat east.
INSERT INTO carrier_appointments (appointment_id, carrier, dock_door, scheduled_arrival, scheduled_departure, status) VALUES
('APPT-TODAY-001', 'ACME Freight',       1, (NOW() + INTERVAL  '2 hours'), (NOW() + INTERVAL  '3 hours'), 'SCHEDULED'),
('APPT-TODAY-002', 'FastShip Logistics', 2, (NOW() + INTERVAL  '4 hours'), (NOW() + INTERVAL  '5 hours'), 'SCHEDULED'),
('APPT-TODAY-003', 'ColdChain Express',  3, (NOW() + INTERVAL  '6 hours'), (NOW() + INTERVAL  '7 hours'), 'SCHEDULED'),
('APPT-TODAY-004', 'RegionalX HazMat',   4, (NOW() + INTERVAL '10 hours'), (NOW() + INTERVAL '11 hours'), 'SCHEDULED');

-- ──────────────────────────────────────────────────────────────────────────────
-- OUTBOUND ORDERS
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS outbound_orders (
    order_id       VARCHAR(50)  PRIMARY KEY,
    appointment_id VARCHAR(50)  NOT NULL REFERENCES carrier_appointments(appointment_id),
    priority       INTEGER      NOT NULL DEFAULT 5,
    cutoff_time    TIMESTAMPTZ  NOT NULL
);

INSERT INTO outbound_orders (order_id, appointment_id, priority, cutoff_time) VALUES
('ORD-0001', 'APPT-TODAY-001', 8,  (NOW() + INTERVAL  '2 hours 30 minutes')),
('ORD-0002', 'APPT-TODAY-001', 7,  (NOW() + INTERVAL  '2 hours 45 minutes')),
('ORD-0003', 'APPT-TODAY-002', 5,  (NOW() + INTERVAL  '4 hours 30 minutes')),
('ORD-0004', 'APPT-TODAY-002', 6,  (NOW() + INTERVAL  '4 hours 45 minutes')),
('ORD-0005', 'APPT-TODAY-003', 4,  (NOW() + INTERVAL  '6 hours 30 minutes')),
('ORD-0006', 'APPT-TODAY-003', 9,  (NOW() + INTERVAL  '6 hours 15 minutes')),
('ORD-0007', 'APPT-TODAY-004', 3,  (NOW() + INTERVAL '10 hours 30 minutes')),
('ORD-0008', 'APPT-TODAY-004', 5,  (NOW() + INTERVAL '10 hours 45 minutes')),
('ORD-0009', 'APPT-TODAY-001', 10, (NOW() + INTERVAL  '2 hours 20 minutes')),
('ORD-0010', 'APPT-TODAY-002', 2,  (NOW() + INTERVAL  '5 hours'));

-- ──────────────────────────────────────────────────────────────────────────────
-- ORDER LINES
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS order_lines (
    line_id    VARCHAR(50) PRIMARY KEY,
    order_id   VARCHAR(50) NOT NULL REFERENCES outbound_orders(order_id),
    sku_id     VARCHAR(50) NOT NULL REFERENCES skus(sku_id),
    quantity   INTEGER     NOT NULL,
    picked     BOOLEAN     NOT NULL DEFAULT FALSE
);

INSERT INTO order_lines (line_id, order_id, sku_id, quantity, picked) VALUES
-- Order 0001: dock 1, priority 8 — fast-moving ambient
('LINE-0001', 'ORD-0001', 'SKU-A001', 20, FALSE),
('LINE-0002', 'ORD-0001', 'SKU-A002',  5, FALSE),
('LINE-0003', 'ORD-0001', 'SKU-A010', 40, FALSE),
-- Order 0002: dock 1, priority 7
('LINE-0004', 'ORD-0002', 'SKU-A003', 80, FALSE),
('LINE-0005', 'ORD-0002', 'SKU-A004', 60, FALSE),
-- Order 0003: dock 2, priority 5 — medium velocity + zone C item (high T_saved)
('LINE-0006', 'ORD-0003', 'SKU-B001', 30, FALSE),
('LINE-0007', 'ORD-0003', 'SKU-B002',  8, FALSE),
('LINE-0008', 'ORD-0003', 'SKU-C001', 10, FALSE),
-- Order 0004: dock 2, priority 6
('LINE-0009', 'ORD-0004', 'SKU-A005', 25, FALSE),
('LINE-0010', 'ORD-0004', 'SKU-B003', 15, FALSE),
-- Order 0005: dock 3, priority 4 — cold chain
('LINE-0011', 'ORD-0005', 'SKU-F001', 50, FALSE),
('LINE-0012', 'ORD-0005', 'SKU-CH001',20, FALSE),
-- Order 0006: dock 3, priority 9 — urgent cold chain
('LINE-0013', 'ORD-0006', 'SKU-F002', 30, FALSE),
('LINE-0014', 'ORD-0006', 'SKU-CH002',25, FALSE),
('LINE-0015', 'ORD-0006', 'SKU-F003', 15, FALSE),
-- Order 0007: dock 4, priority 3 — slow movers from Zone C
('LINE-0016', 'ORD-0007', 'SKU-C002', 10, FALSE),
('LINE-0017', 'ORD-0007', 'SKU-C003',  5, FALSE),
-- Order 0008: dock 4, priority 5 — hazmat
('LINE-0018', 'ORD-0008', 'SKU-HZ001', 4, FALSE),
('LINE-0019', 'ORD-0008', 'SKU-HZ004', 2, FALSE),
-- Order 0009: dock 1, priority 10 — highest urgency
('LINE-0020', 'ORD-0009', 'SKU-A001', 10, FALSE),
('LINE-0021', 'ORD-0009', 'SKU-A011', 15, FALSE),
('LINE-0022', 'ORD-0009', 'SKU-A012',  8, FALSE),
-- Order 0010: dock 2, priority 2 — zone B overflow
('LINE-0023', 'ORD-0010', 'SKU-B008', 30, FALSE),
('LINE-0024', 'ORD-0010', 'SKU-B009', 20, FALSE);
