"""OR-Tools VRPTW route planner for pre-positioning movements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from src.models.inventory import Location
from src.models.movements import MovementTask

logger = structlog.get_logger(__name__)

# Maximum travel time (seconds) used as "infinity" for the routing solver.
_MAX_ROUTE_SECONDS = 86_400  # 24 hours


@dataclass
class Stop:
    """A single stop in a planned route.

    Args:
        task: The movement task executed at this stop.
        arrival_seconds: Estimated seconds from route start when resource arrives.
        departure_seconds: Estimated seconds from route start when resource departs.
    """

    task: MovementTask
    arrival_seconds: float
    departure_seconds: float


@dataclass
class Route:
    """A complete route for a single resource.

    Args:
        resource_id: Identifier of the assigned resource (AGV or forklift).
        stops: Ordered list of stops to execute.
        total_distance_meters: Total Manhattan distance traveled.
        total_time_seconds: Total time from depot departure to final return.
    """

    resource_id: str
    stops: list[Stop] = field(default_factory=list)
    total_distance_meters: float = 0.0
    total_time_seconds: float = 0.0


@dataclass
class RoutingResult:
    """Result from the VRPTW route planner.

    Args:
        routes: One Route per resource that has at least one stop.
        solver_status: OR-Tools routing status string.
        wall_seconds: Elapsed solver wall-clock time in seconds.
    """

    routes: list[Route] = field(default_factory=list)
    solver_status: str = "UNKNOWN"
    wall_seconds: float = 0.0


@dataclass
class GraphEdge:
    """A directed edge in the warehouse graph.

    Args:
        from_node: Origin node ID.
        to_node: Destination node ID.
        distance_meters: Physical distance of the edge in meters.
        speed_mps: Traversal speed in metres per second (allows speed zones).
        one_way: If True, the reverse traversal is not permitted.
    """

    from_node: str
    to_node: str
    distance_meters: float
    speed_mps: float = 2.2
    one_way: bool = False


class WarehouseGraph:
    """Undirected (optionally one-way) graph of warehouse locations.

    Provides travel-time lookups used by the routing solver.

    Args:
        default_speed_mps: Default traversal speed when no edge is present.
    """

    def __init__(self, default_speed_mps: float = 2.2) -> None:
        self._default_speed = default_speed_mps
        self._edges: dict[tuple[str, str], GraphEdge] = {}

    def add_edge(self, edge: GraphEdge) -> None:
        """Register a directed edge.  Adds reverse edge automatically unless one_way.

        Args:
            edge: Edge to add to the graph.
        """
        self._edges[(edge.from_node, edge.to_node)] = edge
        if not edge.one_way:
            reverse = GraphEdge(
                from_node=edge.to_node,
                to_node=edge.from_node,
                distance_meters=edge.distance_meters,
                speed_mps=edge.speed_mps,
                one_way=False,
            )
            self._edges[(edge.to_node, edge.from_node)] = reverse

    def travel_time_seconds(self, from_id: str, to_id: str) -> float:
        """Return travel time in seconds between two location IDs.

        Falls back to Manhattan-distance / default_speed when no explicit edge exists.

        Args:
            from_id: Origin location ID.
            to_id: Destination location ID.

        Returns:
            Travel time in seconds.
        """
        if from_id == to_id:
            return 0.0
        edge = self._edges.get((from_id, to_id))
        if edge is not None:
            return edge.distance_meters / edge.speed_mps
        # No explicit edge — caller should fall back to Manhattan distance.
        return 0.0

    def has_edge(self, from_id: str, to_id: str) -> bool:
        """Return True if an explicit edge exists between the two locations.

        Args:
            from_id: Origin location ID.
            to_id: Destination location ID.

        Returns:
            True if an explicit edge is registered.
        """
        return (from_id, to_id) in self._edges


class MovementRoutePlanner:
    """VRPTW route planner that sequences movement tasks across resources.

    Uses OR-Tools' vehicle routing library (ortools.constraint_solver).
    Each resource starts and ends at a virtual depot node (index 0).
    Tasks are nodes 1..N. Time windows are applied per task based on the
    movement's ``estimated_duration_seconds``.

    Args:
        graph: Optional WarehouseGraph for explicit edge travel times.
            When absent, falls back to Manhattan distance / resource_speed_mps.
        resource_speed_mps: Default travel speed used for Manhattan fallback.
        handling_time_seconds: Fixed dwell time per stop (pick + place).
        solver_timeout_seconds: Wall-clock time limit for the solver.
    """

    def __init__(
        self,
        graph: WarehouseGraph | None = None,
        resource_speed_mps: float = 2.2,
        handling_time_seconds: float = 45.0,
        solver_timeout_seconds: int = 10,
    ) -> None:
        self._graph = graph
        self._speed = resource_speed_mps
        self._handling = handling_time_seconds
        self._timeout = solver_timeout_seconds

    def plan(
        self,
        tasks: list[MovementTask],
        resources: list[str],
        depot_location: Location | None = None,
        time_horizon_seconds: int = 7200,
    ) -> RoutingResult:
        """Compute routes for the given tasks and resources.

        Args:
            tasks: Ordered (by priority) list of movement tasks to route.
            resources: Resource IDs available for routing.
            depot_location: Virtual depot location (start/end for all routes).
                Defaults to the origin of the first task if not provided.
            time_horizon_seconds: Maximum route duration in seconds.

        Returns:
            RoutingResult with one Route per active resource.
        """
        if not tasks or not resources:
            logger.info("routing.empty_input", tasks=len(tasks), resources=len(resources))
            return RoutingResult(solver_status="INFEASIBLE")

        num_vehicles = len(resources)
        # Node 0 = depot; nodes 1..N = tasks.
        num_nodes = len(tasks) + 1

        depot_loc = depot_location or tasks[0].from_location
        # All locations: [depot] + [task.from_location for each task]
        locations: list[Location] = [depot_loc] + [t.from_location for t in tasks]

        # Build integer travel-time matrix (seconds, rounded up).
        time_matrix = _build_time_matrix(locations, self._graph, self._speed)

        # Time windows: depot is [0, horizon]; each task has a window based on its
        # duration estimate (allow starting from 0, must finish within horizon).
        time_windows: list[tuple[int, int]] = [(0, time_horizon_seconds)]
        for task in tasks:
            window_end = min(
                time_horizon_seconds,
                max(int(task.estimated_duration_seconds) + 600, time_horizon_seconds),
            )
            time_windows.append((0, window_end))

        # OR-Tools routing setup.
        manager = pywrapcp.RoutingIndexManager(num_nodes, num_vehicles, 0)
        routing = pywrapcp.RoutingModel(manager)

        # Transit callback.
        def _transit(from_idx: int, to_idx: int) -> int:
            from_node = manager.IndexToNode(from_idx)
            to_node = manager.IndexToNode(to_idx)
            return time_matrix[from_node][to_node]

        transit_callback_index = routing.RegisterTransitCallback(_transit)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

        # Time dimension with time windows.
        routing.AddDimension(
            transit_callback_index,
            slack_max=0,
            capacity=time_horizon_seconds,
            fix_start_cumul_to_zero=True,
            name="Time",
        )
        time_dim = routing.GetDimensionOrDie("Time")

        # Apply time windows.
        for node_idx in range(1, num_nodes):
            index = manager.NodeToIndex(node_idx)
            tw = time_windows[node_idx]
            time_dim.CumulVar(index).SetRange(tw[0], tw[1])

        # Solve.
        search_params = pywrapcp.DefaultRoutingSearchParameters()
        search_params.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        search_params.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        search_params.time_limit.seconds = self._timeout
        search_params.log_search = False

        solution = routing.SolveWithParameters(search_params)

        # OR-Tools routing status integers (stable across 9.x).
        _STATUS_NAMES: dict[int, str] = {
            0: "ROUTING_NOT_SOLVED",
            1: "ROUTING_SUCCESS",
            2: "ROUTING_PARTIAL_SUCCESS",
            3: "ROUTING_FAIL",
            4: "ROUTING_FAIL_TIMEOUT",
            5: "ROUTING_INVALID",
            6: "ROUTING_INFEASIBLE",
        }
        raw_status = routing.status()
        status_name = _STATUS_NAMES.get(raw_status, f"ROUTING_STATUS_{raw_status}")

        logger.info(
            "routing.solved",
            status=status_name,
            tasks=len(tasks),
            resources=num_vehicles,
        )

        if solution is None:
            return RoutingResult(solver_status=status_name)

        routes = _extract_routes(
            solution,
            routing,
            manager,
            tasks,
            resources,
            time_dim,
            locations,
            self._speed,
            self._handling,
        )

        return RoutingResult(routes=routes, solver_status=status_name)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _manhattan(loc_a: Location, loc_b: Location) -> float:
    """Manhattan distance in meters between two locations.

    Args:
        loc_a: First location.
        loc_b: Second location.

    Returns:
        Distance in meters.
    """
    return abs(loc_a.x - loc_b.x) + abs(loc_a.y - loc_b.y)


def _build_time_matrix(
    locations: list[Location],
    graph: WarehouseGraph | None,
    speed_mps: float,
) -> list[list[int]]:
    """Build an N×N integer travel-time matrix (seconds).

    Uses explicit graph edges where available; falls back to Manhattan distance.

    Args:
        locations: Ordered list of locations (depot first).
        graph: Optional WarehouseGraph with explicit edges.
        speed_mps: Fallback speed for Manhattan distance calculations.

    Returns:
        N×N matrix of integer travel times in seconds.
    """
    n = len(locations)
    matrix: list[list[int]] = [[0] * n for _ in range(n)]

    for i in range(n):
        for j in range(n):
            if i == j:
                matrix[i][j] = 0
                continue
            loc_a = locations[i]
            loc_b = locations[j]
            if graph is not None and graph.has_edge(loc_a.location_id, loc_b.location_id):
                t = graph.travel_time_seconds(loc_a.location_id, loc_b.location_id)
            else:
                dist = _manhattan(loc_a, loc_b)
                t = dist / speed_mps if speed_mps > 0 else 0.0
            matrix[i][j] = max(1, int(t))

    return matrix


def _extract_routes(
    solution: Any,
    routing: pywrapcp.RoutingModel,
    manager: pywrapcp.RoutingIndexManager,
    tasks: list[MovementTask],
    resources: list[str],
    time_dim: Any,
    locations: list[Location],
    speed_mps: float,
    handling_seconds: float,
) -> list[Route]:
    """Extract Route objects from a solved OR-Tools solution.

    Args:
        solution: OR-Tools assignment solution.
        routing: The RoutingModel used to solve.
        manager: RoutingIndexManager for node/index conversions.
        tasks: Original task list (node 1 = tasks[0], etc.).
        resources: Resource IDs in vehicle order.
        time_dim: Time dimension from the routing model.
        locations: Location list used when building the time matrix.
        speed_mps: Resource speed for distance calculations.
        handling_seconds: Fixed dwell time per stop.

    Returns:
        List of Route objects, one per vehicle that has at least one stop.
    """
    routes: list[Route] = []

    for v_idx, resource_id in enumerate(resources):
        index = routing.Start(v_idx)
        stops: list[Stop] = []
        total_dist = 0.0
        prev_loc: Location | None = None

        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node != 0:
                # node > 0 → task at index (node - 1)
                task = tasks[node - 1]
                arrival = solution.Min(time_dim.CumulVar(index))
                departure = arrival + int(handling_seconds)
                stop = Stop(
                    task=task,
                    arrival_seconds=float(arrival),
                    departure_seconds=float(departure),
                )
                stops.append(stop)
                if prev_loc is not None:
                    total_dist += _manhattan(prev_loc, task.from_location)
                prev_loc = task.to_location

            next_index = solution.Value(routing.NextVar(index))
            index = next_index

        if stops:
            end_time = solution.Min(time_dim.CumulVar(routing.End(v_idx)))
            routes.append(
                Route(
                    resource_id=resource_id,
                    stops=stops,
                    total_distance_meters=total_dist,
                    total_time_seconds=float(end_time),
                )
            )

    return routes
