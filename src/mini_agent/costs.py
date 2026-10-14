"""Fail-closed client cost budget, including uncertain requests and retries."""

from decimal import Decimal
from urllib.parse import urlsplit

from .models import ToolError


class CostBudget:
    def __init__(self, config):
        self.limit = Decimal(str(config.max_run_cost_cny))
        self.input_price = config.input_price_cny_per_million
        self.output_price = config.output_price_cny_per_million
        if self.input_price is None and (urlsplit(config.endpoint).hostname == "api.deepseek.com"
                                         and config.model == "deepseek-flash"):
            # Official peak rates, cache misses: checked 2026-10-14. Recheck after price changes.
            self.input_price, self.output_price = 2, 8
        self.used = Decimal(0)
        self.attempts = []
        self.blocked = False

    def _cost(self, input_tokens, output_tokens):
        return (Decimal(input_tokens) * Decimal(str(self.input_price))
                + Decimal(output_tokens) * Decimal(str(self.output_price))) / Decimal(1_000_000)

    def reserve(self, payload, output_limit):
        if self.blocked:
            raise ToolError("MODEL_COST_ESTIMATE_EXCEEDED", "Cost estimation is unreliable for this run; no further request sent")
        if self.input_price is None:
            raise ToolError("MODEL_PRICING_UNKNOWN", "Configure verified CNY input/output prices before model requests")
        # UTF-8 bytes overcount ordinary BPE text; include serialized schema and framing margin.
        input_bound = len(payload) + 1024
        amount = self._cost(input_bound, output_limit)
        if self.used + amount > self.limit:
            raise ToolError("MODEL_COST_LIMIT", "Next request (including maximum output) would exceed the CNY run budget; no request sent")
        self.used += amount
        record = {"input_token_bound": input_bound, "max_output_tokens": output_limit,
                  "reserved_cny": str(amount), "accounted_cny": str(amount), "usage_known": False}
        self.attempts.append(record)
        return record

    def settle(self, record, input_tokens, output_tokens):
        amount = self._cost(input_tokens, output_tokens)
        reserved = Decimal(record["reserved_cny"])
        self.used += amount - reserved
        record.update(usage_known=True, input_tokens=input_tokens, output_tokens=output_tokens,
                      accounted_cny=str(amount))
        if amount > reserved or self.used > self.limit:
            self.blocked = True
            raise ToolError("MODEL_COST_ESTIMATE_EXCEEDED", "Provider usage exceeded the reserved estimate; further actions stopped")

    def summary(self):
        return {"currency": "CNY", "limit_cny": str(self.limit),
                "accounted_upper_estimate_cny": str(self.used),
                "remaining_cny": str(max(Decimal(0), self.limit - self.used)),
                "input_price_cny_per_million": self.input_price,
                "output_price_cny_per_million": self.output_price,
                "uncertain_requests": sum(not r["usage_known"] for r in self.attempts),
                "attempts": self.attempts,
                "billing_verified": False}
