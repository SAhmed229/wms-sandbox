"""Generic PostgreSQL WMS adapter with Redis caching."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.ingestion.schema import WMSSchema
from src.ingestion.wms_adapter import WMSAdapter
from src.models.inventory import (
    ABCClass,
    HazmatClass,
    InventoryPosition,
    Location,
    SKU,
    TemperatureZone,
)
from src.models.orders import AppointmentStatus, CarrierAppointment, OrderLine, OutboundOrder

logger = structlog.get_logger(__name__)


def _parse_datetime(value: Any) -> datetime:
    """Parse a datetime value from DB row.

    Args:
        value: Raw datetime value from DB.

    Returns:
        UTC-aware datetime.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)


def _row_to_location(row: Any) -> Location:
    """Convert a DB row mapping (with canonical column aliases) to a Location.

    Args:
        row: Row mapping from SQLAlchemy result.

    Returns:
        Location model instance.
    """
    return Location(
        location_id=str(row["location_id"]),
        zone=str(row["zone"]),
        aisle=int(row["aisle"]),
        bay=int(row["bay"]),
        level=int(row["level"]),
        x=float(row["x"]),
        y=float(row["y"]),
        temperature_zone=TemperatureZone(row.get("temperature_zone", "AMBIENT")),
        max_weight_kg=float(row.get("max_weight_kg", 2000.0)),
        max_volume_m3=float(row.get("max_volume_m3", 10.0)),
        is_staging=bool(row.get("is_staging", False)),
        nearest_dock_door=row.get("nearest_dock_door"),
    )


def _row_to_sku(row: Any) -> SKU:
    """Convert a DB row mapping (with canonical column aliases) to a SKU.

    Args:
        row: Row mapping from SQLAlchemy result.

    Returns:
        SKU model instance.
    """
    hazmat_raw = row.get("hazmat_class")
    hazmat_class = HazmatClass(hazmat_raw) if hazmat_raw else None
    return SKU(
        sku_id=str(row["sku_id"]),
        description=str(row.get("description", "")),
        weight_kg=float(row.get("weight_kg", 0.0)),
        volume_m3=float(row.get("volume_m3", 0.0)),
        hazmat_class=hazmat_class,
        requires_temperature_zone=TemperatureZone(
            row.get("requires_temperature_zone", "AMBIENT")
        ),
        abc_class=ABCClass(row.get("abc_class", "C")),
    )


_LOCATION_COLS = [
    "location_id",
    "zone",
    "aisle",
    "bay",
    "level",
    "x",
    "y",
    "temperature_zone",
    "max_weight_kg",
    "max_volume_m3",
    "is_staging",
    "nearest_dock_door",
]
_SKU_COLS = [
    "sku_id",
    "description",
    "weight_kg",
    "volume_m3",
    "hazmat_class",
    "requires_temperature_zone",
    "abc_class",
]
_INVENTORY_OWN_COLS = ["position_id", "quantity", "lot_number", "expiry_date"]
_APPOINTMENT_COLS = [
    "appointment_id",
    "carrier",
    "dock_door",
    "scheduled_arrival",
    "scheduled_departure",
    "status",
]


class GenericDBAdapter(WMSAdapter):
    """WMS adapter that reads from PostgreSQL with Redis caching.

    Args:
        database_url: Async SQLAlchemy database URL.
        redis_client: Connected redis.asyncio.Redis client.
        cache_ttl_seconds: How long to cache results in Redis.
        schema: WMSSchema with customer table/column name overrides.
            Defaults to canonical names matching scripts/init_db.sql.
    """

    def __init__(
        self,
        database_url: str,
        redis_client: Any,
        cache_ttl_seconds: int = 60,
        schema: WMSSchema | None = None,
    ) -> None:
        self._database_url = database_url
        self._redis = redis_client
        self._cache_ttl = cache_ttl_seconds
        self._schema = schema or WMSSchema()
        self._engine: AsyncEngine | None = None
        self._session_factory: Any = None

    async def connect(self) -> None:
        """Initialize the database connection pool."""
        self._engine = create_async_engine(self._database_url, pool_size=5, max_overflow=10)
        self._session_factory = sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )
        logger.info("generic_db_adapter.connected", database_url=self._database_url)

    async def disconnect(self) -> None:
        """Close the database connection pool."""
        if self._engine:
            await self._engine.dispose()
            logger.info("generic_db_adapter.disconnected")

    async def _get_cached(self, key: str) -> Any | None:
        """Retrieve a cached JSON value from Redis."""
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(key)
            if raw:
                return json.loads(raw)
        except Exception as exc:
            logger.warning("cache.get_failed", key=key, error=str(exc))
        return None

    async def _set_cached(self, key: str, value: Any) -> None:
        """Store a JSON-serializable value in Redis with TTL."""
        if self._redis is None:
            return
        try:
            await self._redis.setex(key, self._cache_ttl, json.dumps(value, default=str))
        except Exception as exc:
            logger.warning("cache.set_failed", key=key, error=str(exc))

    def _get_session(self) -> AsyncSession:
        """Create a new async database session."""
        if self._session_factory is None:
            raise RuntimeError("Adapter not connected. Call connect() first.")
        return self._session_factory()

    async def get_inventory_positions(
        self, zone: str | None = None
    ) -> list[InventoryPosition]:
        """Fetch inventory positions from DB, with Redis caching.

        Args:
            zone: Optional zone filter.

        Returns:
            List of InventoryPosition instances.
        """
        cache_key = f"wms:inventory:{zone or 'all'}"
        cached = await self._get_cached(cache_key)
        if cached is not None:
            logger.debug("cache.hit", key=cache_key)
            return [InventoryPosition.model_validate(item) for item in cached]

        t0 = time.perf_counter()
        sch = self._schema
        locs_tbl = sch.table("locations")
        skus_tbl = sch.table("skus")
        inv_tbl = sch.table("inventory_positions")

        ip_location_fk = sch.col("inventory_positions", "location_id")
        ip_sku_fk = sch.col("inventory_positions", "sku_id")
        l_pk = sch.col("locations", "location_id")
        s_pk = sch.col("skus", "sku_id")
        l_zone = sch.col("locations", "zone")

        select_ip = sch.select_clause("inventory_positions", _INVENTORY_OWN_COLS, alias="ip")
        select_loc = sch.select_clause("locations", _LOCATION_COLS, alias="l")
        select_sku = sch.select_clause("skus", _SKU_COLS, alias="s")

        zone_clause = f"WHERE l.{l_zone} = :zone" if zone else ""
        query = text(f"""
            SELECT {select_ip}, {select_loc}, {select_sku}
            FROM {inv_tbl} ip
            JOIN {locs_tbl} l ON ip.{ip_location_fk} = l.{l_pk}
            JOIN {skus_tbl} s ON ip.{ip_sku_fk} = s.{s_pk}
            {zone_clause}
        """)

        positions: list[InventoryPosition] = []
        async with self._get_session() as session:
            params: dict[str, Any] = {}
            if zone:
                params["zone"] = zone
            result = await session.execute(query, params)
            rows = result.mappings().all()

        duration = time.perf_counter() - t0
        logger.info(
            "wms.poll",
            table="inventory_positions",
            rows=len(rows),
            duration_seconds=round(duration, 3),
        )

        for row in rows:
            loc = _row_to_location(row)
            sku = _row_to_sku(row)
            positions.append(
                InventoryPosition(
                    position_id=str(row["position_id"]),
                    sku=sku,
                    location=loc,
                    quantity=int(row["quantity"]),
                    lot_number=row.get("lot_number"),
                    expiry_date=_parse_datetime(row["expiry_date"]) if row.get("expiry_date") else None,
                )
            )

        await self._set_cached(cache_key, [p.model_dump(mode="json") for p in positions])
        return positions

    async def get_outbound_orders(
        self, horizon_hours: float = 24
    ) -> list[OutboundOrder]:
        """Fetch outbound orders with cutoff within horizon.

        Args:
            horizon_hours: Planning horizon in hours.

        Returns:
            List of OutboundOrder instances.
        """
        cache_key = f"wms:orders:{int(horizon_hours)}"
        cached = await self._get_cached(cache_key)
        if cached is not None:
            return [OutboundOrder.model_validate(item) for item in cached]

        t0 = time.perf_counter()
        cutoff = datetime.now(UTC) + timedelta(hours=horizon_hours)
        sch = self._schema
        orders_tbl = sch.table("outbound_orders")
        lines_tbl = sch.table("order_lines")
        appts_tbl = sch.table("carrier_appointments")

        o_appt_fk = sch.col("outbound_orders", "appointment_id")
        a_pk = sch.col("carrier_appointments", "appointment_id")
        ol_order_fk = sch.col("order_lines", "order_id")
        o_pk = sch.col("outbound_orders", "order_id")
        o_cutoff = sch.col("outbound_orders", "cutoff_time")
        o_order_pk_alias = sch.col("outbound_orders", "order_id")
        ol_line_pk = sch.col("order_lines", "line_id")

        select_order = sch.select_clause(
            "outbound_orders", ["order_id", "priority", "cutoff_time"], alias="o"
        )
        # appointment.status would alias to canonical 'status', but the row mapper
        # uses 'appt_status' to avoid collision — rename the output for this query.
        select_appt = sch.select_clause(
            "carrier_appointments",
            _APPOINTMENT_COLS,
            alias="a",
            rename={"status": "appt_status"},
        )
        select_line = sch.select_clause(
            "order_lines", ["line_id", "sku_id", "quantity", "picked"], alias="ol"
        )

        query = text(f"""
            SELECT {select_order}, {select_appt}, {select_line}
            FROM {orders_tbl} o
            JOIN {appts_tbl} a ON o.{o_appt_fk} = a.{a_pk}
            JOIN {lines_tbl} ol ON ol.{ol_order_fk} = o.{o_pk}
            WHERE o.{o_cutoff} <= :cutoff
            ORDER BY o.{o_order_pk_alias}, ol.{ol_line_pk}
        """)

        async with self._get_session() as session:
            result = await session.execute(query, {"cutoff": cutoff})
            rows = result.mappings().all()

        duration = time.perf_counter() - t0
        logger.info("wms.poll", table="outbound_orders", rows=len(rows), duration_seconds=round(duration, 3))

        orders_map: dict[str, OutboundOrder] = {}
        for row in rows:
            oid = str(row["order_id"])
            if oid not in orders_map:
                appt = CarrierAppointment(
                    appointment_id=str(row["appointment_id"]),
                    carrier=str(row["carrier"]),
                    dock_door=int(row["dock_door"]),
                    scheduled_arrival=_parse_datetime(row["scheduled_arrival"]),
                    scheduled_departure=_parse_datetime(row["scheduled_departure"]),
                    status=AppointmentStatus(row["appt_status"]),
                )
                orders_map[oid] = OutboundOrder(
                    order_id=oid,
                    appointment=appt,
                    lines=[],
                    priority=int(row["priority"]),
                    cutoff_time=_parse_datetime(row["cutoff_time"]),
                )
            orders_map[oid].lines.append(
                OrderLine(
                    line_id=str(row["line_id"]),
                    sku_id=str(row["sku_id"]),
                    quantity=int(row["quantity"]),
                    picked=bool(row["picked"]),
                )
            )

        orders = sorted(orders_map.values(), key=lambda o: o.cutoff_time)
        await self._set_cached(cache_key, [o.model_dump(mode="json") for o in orders])
        return orders

    async def get_carrier_appointments(
        self, horizon_hours: float = 24
    ) -> list[CarrierAppointment]:
        """Fetch carrier appointments within the planning horizon.

        Args:
            horizon_hours: Planning horizon in hours.

        Returns:
            List of CarrierAppointment instances sorted by scheduled_arrival.
        """
        cache_key = f"wms:appointments:{int(horizon_hours)}"
        cached = await self._get_cached(cache_key)
        if cached is not None:
            return [CarrierAppointment.model_validate(item) for item in cached]

        t0 = time.perf_counter()
        cutoff = datetime.now(UTC) + timedelta(hours=horizon_hours)
        sch = self._schema
        appts_tbl = sch.table("carrier_appointments")
        a_arrival = sch.col("carrier_appointments", "scheduled_arrival")
        select_appt = sch.select_clause("carrier_appointments", _APPOINTMENT_COLS)

        query = text(f"""
            SELECT {select_appt}
            FROM {appts_tbl}
            WHERE {a_arrival} <= :cutoff
            ORDER BY {a_arrival}
        """)

        async with self._get_session() as session:
            result = await session.execute(query, {"cutoff": cutoff})
            rows = result.mappings().all()

        duration = time.perf_counter() - t0
        logger.info("wms.poll", table="carrier_appointments", rows=len(rows), duration_seconds=round(duration, 3))

        appointments = [
            CarrierAppointment(
                appointment_id=str(row["appointment_id"]),
                carrier=str(row["carrier"]),
                dock_door=int(row["dock_door"]),
                scheduled_arrival=_parse_datetime(row["scheduled_arrival"]),
                scheduled_departure=_parse_datetime(row["scheduled_departure"]),
                status=AppointmentStatus(row["status"]),
            )
            for row in rows
        ]

        await self._set_cached(cache_key, [a.model_dump(mode="json") for a in appointments])
        return appointments

    async def get_staging_locations(
        self, dock_door: int | None = None
    ) -> list[Location]:
        """Fetch staging locations, optionally for a specific dock door.

        Args:
            dock_door: Optional dock door filter.

        Returns:
            List of staging Location instances.
        """
        cache_key = f"wms:staging:{dock_door or 'all'}"
        cached = await self._get_cached(cache_key)
        if cached is not None:
            return [Location.model_validate(item) for item in cached]

        sch = self._schema
        locs_tbl = sch.table("locations")
        is_staging_col = sch.col("locations", "is_staging")
        dock_door_col = sch.col("locations", "nearest_dock_door")
        select_loc = sch.select_clause("locations", _LOCATION_COLS)

        door_clause = f"AND {dock_door_col} = :dock_door" if dock_door else ""
        query = text(f"""
            SELECT {select_loc}
            FROM {locs_tbl}
            WHERE {is_staging_col} = TRUE {door_clause}
        """)

        params: dict[str, Any] = {}
        if dock_door:
            params["dock_door"] = dock_door

        async with self._get_session() as session:
            result = await session.execute(query, params)
            rows = result.mappings().all()

        locations = [_row_to_location(row) for row in rows]
        await self._set_cached(cache_key, [loc.model_dump(mode="json") for loc in locations])
        return locations

    async def get_location_utilization(self) -> dict[str, float]:
        """Compute fill fraction for each location based on current inventory.

        Returns:
            Map of location_id to utilization fraction [0.0, 1.0].
        """
        cache_key = "wms:utilization"
        cached = await self._get_cached(cache_key)
        if cached is not None:
            return cached

        sch = self._schema
        inv_tbl = sch.table("inventory_positions")
        locs_tbl = sch.table("locations")
        skus_tbl = sch.table("skus")

        l_pk = sch.col("locations", "location_id")
        l_max_w = sch.col("locations", "max_weight_kg")
        l_max_v = sch.col("locations", "max_volume_m3")
        ip_loc_fk = sch.col("inventory_positions", "location_id")
        ip_sku_fk = sch.col("inventory_positions", "sku_id")
        ip_qty = sch.col("inventory_positions", "quantity")
        s_pk = sch.col("skus", "sku_id")
        s_weight = sch.col("skus", "weight_kg")
        s_volume = sch.col("skus", "volume_m3")

        query = text(f"""
            SELECT
                l.{l_pk} AS location_id,
                l.{l_max_w} AS max_weight_kg,
                l.{l_max_v} AS max_volume_m3,
                COALESCE(SUM(ip.{ip_qty} * s.{s_weight}), 0) AS total_weight,
                COALESCE(SUM(ip.{ip_qty} * s.{s_volume}), 0) AS total_volume
            FROM {locs_tbl} l
            LEFT JOIN {inv_tbl} ip ON ip.{ip_loc_fk} = l.{l_pk}
            LEFT JOIN {skus_tbl} s ON s.{s_pk} = ip.{ip_sku_fk}
            GROUP BY l.{l_pk}, l.{l_max_w}, l.{l_max_v}
        """)

        async with self._get_session() as session:
            result = await session.execute(query)
            rows = result.mappings().all()

        utilization: dict[str, float] = {}
        for row in rows:
            max_w = float(row["max_weight_kg"]) or 1.0
            max_v = float(row["max_volume_m3"]) or 1.0
            weight_util = float(row["total_weight"]) / max_w
            volume_util = float(row["total_volume"]) / max_v
            utilization[str(row["location_id"])] = min(1.0, max(weight_util, volume_util))

        await self._set_cached(cache_key, utilization)
        return utilization
