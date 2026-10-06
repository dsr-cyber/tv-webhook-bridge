from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Alert(BaseModel):
    """One TradingView alert. Field names match what you put in the alert message box."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    secret: str = Field(repr=False)
    alert_id: str = Field(min_length=1, max_length=80)
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.]{0,9}$")
    side: Literal["buy", "sell"]
    qty: int = Field(gt=0, le=100_000)
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    timestamp: datetime | None = None

    @field_validator("symbol", "side", "order_type", mode="before")
    @classmethod
    def _normalise_case(cls, v, info):
        if not isinstance(v, str):
            return v
        v = v.strip()
        # TradingView's {{ticker}} is bare, but people paste "NASDAQ:AAPL" from the chart header
        if info.field_name == "symbol":
            return v.split(":")[-1].upper()
        return v.lower()

    @field_validator("timestamp")
    @classmethod
    def _assume_utc(cls, v: datetime | None):
        # {{timenow}} is UTC with a Z, but a hand-written test alert often has no offset
        if v is not None and v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v

    @model_validator(mode="after")
    def _check_prices(self):
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit orders need limit_price")
        if self.order_type == "market" and self.limit_price is not None:
            raise ValueError("limit_price given on a market order; set order_type to limit")

        sl, tp = self.stop_loss, self.take_profit
        if sl is not None and tp is not None:
            if self.side == "buy" and not sl < tp:
                raise ValueError("buy bracket needs stop_loss < take_profit")
            if self.side == "sell" and not sl > tp:
                raise ValueError("sell bracket needs stop_loss > take_profit")

        ref = self.limit_price
        if ref is not None:
            below, above = (sl, tp) if self.side == "buy" else (tp, sl)
            if below is not None and below >= ref:
                raise ValueError("stop/target on the wrong side of limit_price")
            if above is not None and above <= ref:
                raise ValueError("stop/target on the wrong side of limit_price")
        return self

    @property
    def has_bracket(self) -> bool:
        return self.stop_loss is not None or self.take_profit is not None

    def public_dict(self) -> dict:
        return self.model_dump(mode="json", exclude={"secret"})
