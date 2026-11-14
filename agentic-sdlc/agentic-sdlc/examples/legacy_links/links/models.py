from dataclasses import dataclass


@dataclass
class Link:
    code: str
    target_url: str
    created_at: float
    clicks: int = 0
