"""
Browseterm allocation vs. physical host capacity (plan section: "Resource allocation model").

PHYSICAL HOST CAPACITY (total_*) and BROWSETERM ALLOCATION (allocated_*) are distinct;
`available_*` (allocated - used) is derived, never stored here or in the DB (P01's own design).

This is defense-in-depth only - P05's Cloud API performs the authoritative validation
server-side. Desktop validation exists so a user gets immediate feedback without a round trip,
not as a replacement for it.
"""
from dataclasses import dataclass

from desktop.hardware import HostResources


class AllocationValidationError(ValueError):
    """Raised with every violated rule joined into one message."""


@dataclass(frozen=True)
class Allocation:
    allocated_cpu: int
    allocated_memory_bytes: int
    allocated_storage_bytes: int


def validate_allocation(allocation: Allocation, total: HostResources) -> None:
    errors: list[str] = []

    if allocation.allocated_cpu < 0:
        errors.append("allocated_cpu must be >= 0")
    if allocation.allocated_memory_bytes < 0:
        errors.append("allocated_memory_bytes must be >= 0")
    if allocation.allocated_storage_bytes < 0:
        errors.append("allocated_storage_bytes must be >= 0")

    if allocation.allocated_cpu > total.total_cpu:
        errors.append(
            f"allocated_cpu ({allocation.allocated_cpu}) exceeds detected total CPU "
            f"({total.total_cpu})"
        )
    if allocation.allocated_memory_bytes > total.total_memory_bytes:
        errors.append(
            f"allocated_memory_bytes ({allocation.allocated_memory_bytes}) exceeds detected "
            f"total memory ({total.total_memory_bytes})"
        )
    if allocation.allocated_storage_bytes > total.total_storage_bytes:
        errors.append(
            f"allocated_storage_bytes ({allocation.allocated_storage_bytes}) exceeds permitted "
            f"storage capacity ({total.total_storage_bytes})"
        )

    if errors:
        raise AllocationValidationError("; ".join(errors))
