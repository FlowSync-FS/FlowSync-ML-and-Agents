"""Cold-chain business logic."""

import logging

logger = logging.getLogger("flowsync.services.cold_chain")


async def evaluate_temperature_excursion(
    depot_id: str,
    temperature_c: float,
    min_allowed_c: float,
    max_allowed_c: float,
) -> dict[str, object]:
    """Evaluate whether a reading violates configured temperature bounds."""
    is_excursion = temperature_c < min_allowed_c or temperature_c > max_allowed_c
    logger.info(f"[{depot_id}] cold_chain: temp={temperature_c}, excursion={is_excursion}")
    return {
        "depot_id": depot_id,
        "temperature_c": temperature_c,
        "is_excursion": is_excursion,
    }
