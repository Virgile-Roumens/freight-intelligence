import numpy as np
import pandas as pd

from src.config import TRADE_ROUTES, VESSEL_SPECS


class TCECalculator:
    """
    Time Charter Equivalent calculator.
    Formula (CM Navigator): TCE = (Gross Freight - Voyage Costs) / Voyage Duration
    Pure calculation — no I/O or external calls.
    """

    VESSEL_TYPES = list(VESSEL_SPECS.keys())

    # Canal tolls (USD, approximate 2024 levels)
    CANAL_TOLLS = {
        "Suez":   {"Capesize": 450_000, "Panamax": 280_000, "Supramax": 210_000, "Handysize": 140_000},
        "Panama": {"Capesize": 0,       "Panamax": 350_000, "Supramax": 180_000, "Handysize": 120_000},
    }

    def calculate_tce(
        self,
        vessel_type: str,
        cargo_tons: float,
        freight_rate_usd_ton: float,
        laden_distance_nm: float,
        ballast_distance_nm: float,
        speed_knots: float,
        bunker_price_usd_mt: float,
        port_costs_total: float,
        commissions_pct: float = 5.0,
        laytime_days: float = 3.0,
        demurrage_days: float = 0.0,
        demurrage_rate: float = 0.0,
        canal_tolls: float = 0.0,
    ) -> dict:
        """
        Returns full voyage economics breakdown.
        Laden and ballast legs each use vessel's respective consumption rates.
        """
        spec = VESSEL_SPECS.get(vessel_type)
        if spec is None:
            raise ValueError(f"Unknown vessel type: {vessel_type}")

        # Voyage durations (days)
        sea_days_laden   = laden_distance_nm / (speed_knots * 24)
        sea_days_ballast = ballast_distance_nm / (speed_knots * 24)
        sea_days_total   = sea_days_laden + sea_days_ballast
        port_days        = laytime_days  # includes load and discharge
        total_voyage_days = sea_days_total + port_days

        # Speed adjustment factor (cubic law)
        # Reference speed for consumption specs is typical speed
        ref_speed = spec["typical_speed_knots"]
        speed_factor_laden   = (speed_knots / ref_speed) ** 3
        speed_factor_ballast = (speed_knots / ref_speed) ** 3

        # Bunker consumption
        bunker_laden   = sea_days_laden   * spec["consumption_laden_mt_day"]   * speed_factor_laden
        bunker_ballast = sea_days_ballast * spec["consumption_ballast_mt_day"] * speed_factor_ballast
        bunker_port    = port_days        * spec["consumption_port_mt_day"]
        bunker_total   = bunker_laden + bunker_ballast + bunker_port
        bunker_cost    = bunker_total * bunker_price_usd_mt

        # Revenue
        gross_freight = cargo_tons * freight_rate_usd_ton
        demurrage_rev = demurrage_days * demurrage_rate

        # Deductions
        commission_cost = gross_freight * (commissions_pct / 100)

        # Total voyage costs
        voyage_costs = bunker_cost + port_costs_total + commission_cost + canal_tolls

        # Net revenue
        net_voyage_revenue = gross_freight - voyage_costs + demurrage_rev

        # TCE
        if total_voyage_days <= 0:
            tce = 0.0
        else:
            tce = net_voyage_revenue / total_voyage_days

        # P&L vs OPEX
        opex = spec["typical_opex_usd_day"]
        daily_margin = tce - opex

        return {
            "vessel_type":          vessel_type,
            "cargo_tons":           cargo_tons,
            "laden_nm":             laden_distance_nm,
            "ballast_nm":           ballast_distance_nm,
            "speed_knots":          speed_knots,
            "gross_freight_usd":    gross_freight,
            "bunker_cost_usd":      bunker_cost,
            "bunker_consumed_mt":   bunker_total,
            "port_cost_usd":        port_costs_total,
            "canal_tolls_usd":      canal_tolls,
            "commission_usd":       commission_cost,
            "demurrage_revenue":    demurrage_rev,
            "voyage_costs_total":   voyage_costs,
            "net_voyage_revenue":   net_voyage_revenue,
            "sea_days_laden":       sea_days_laden,
            "sea_days_ballast":     sea_days_ballast,
            "port_days":            port_days,
            "total_voyage_days":    total_voyage_days,
            "tce_usd_day":          tce,
            "opex_usd_day":         opex,
            "daily_margin_usd":     daily_margin,
            "bunker_pct_of_costs":  (bunker_cost / voyage_costs * 100) if voyage_costs > 0 else 0,
        }

    def breakeven_freight_rate(
        self,
        vessel_type: str,
        cargo_tons: float,
        laden_nm: float,
        ballast_nm: float,
        speed_knots: float,
        bunker_price: float,
        port_costs: float,
        commissions_pct: float = 5.0,
        laytime_days: float = 3.0,
        tce_target_usd_day: float = 0.0,
    ) -> float:
        """
        Returns the freight rate (USD/ton) needed to achieve the target TCE.
        Solves: target_TCE × voyage_days + voyage_costs = gross_freight
        """
        spec = VESSEL_SPECS[vessel_type]
        ref_speed = spec["typical_speed_knots"]
        sf = (speed_knots / ref_speed) ** 3
        sea_laden   = laden_nm   / (speed_knots * 24)
        sea_ballast = ballast_nm / (speed_knots * 24)
        total_days  = sea_laden + sea_ballast + laytime_days
        bunker_cost = (
            sea_laden   * spec["consumption_laden_mt_day"]   * sf +
            sea_ballast * spec["consumption_ballast_mt_day"] * sf +
            laytime_days * spec["consumption_port_mt_day"]
        ) * bunker_price
        fixed_costs = bunker_cost + port_costs
        target_net  = tce_target_usd_day * total_days + fixed_costs
        # target_net = gross_freight * (1 - commissions/100)
        gross_needed = target_net / (1 - commissions_pct / 100)
        return gross_needed / cargo_tons if cargo_tons > 0 else 0.0

    def sensitivity_analysis(
        self,
        base_params: dict,
        variable: str,
        range_pct: float = 0.30,
        steps: int = 20,
    ) -> pd.DataFrame:
        """
        Varies one parameter ±range_pct and returns TCE impacts.
        variable: one of 'bunker_price_usd_mt', 'freight_rate_usd_ton', 'speed_knots'
        """
        base_val = base_params.get(variable)
        if base_val is None:
            raise ValueError(f"Parameter {variable} not in base_params")

        results = []
        for i in range(steps + 1):
            factor = (1 - range_pct) + (2 * range_pct * i / steps)
            new_val = base_val * factor
            params = {**base_params, variable: new_val}
            try:
                result = self.calculate_tce(**params)
                results.append({
                    "variable":    variable,
                    "value":       new_val,
                    "pct_change":  (factor - 1) * 100,
                    "tce":         result["tce_usd_day"],
                    "tce_delta":   result["tce_usd_day"] - self.calculate_tce(**base_params)["tce_usd_day"],
                })
            except Exception:
                pass
        return pd.DataFrame(results)

    @staticmethod
    def get_route_params(route_key: str) -> dict | None:
        """Returns pre-filled parameters for a known trade route."""
        route = TRADE_ROUTES.get(route_key)
        if not route:
            return None
        return {
            "vessel_type":          route["segment"],
            "cargo_tons":           route["typical_cargo_mt"],
            "laden_distance_nm":    route["laden_nm"],
            "ballast_distance_nm":  route["ballast_nm"],
            "speed_knots":          VESSEL_SPECS[route["segment"]]["typical_speed_knots"],
            "laytime_days":         route.get("typical_port_days", 3.0),
            "port_costs_total":     VESSEL_SPECS[route["segment"]]["typical_opex_usd_day"] * 1.5 * 2,
        }

    @staticmethod
    def format_breakdown_table(result: dict) -> pd.DataFrame:
        """Returns a formatted cost breakdown as a display DataFrame."""
        rows = [
            ("Gross Freight Revenue",     f"${result['gross_freight_usd']:,.0f}"),
            ("— Commission",              f"-${result['commission_usd']:,.0f}"),
            ("— Bunker Cost",             f"-${result['bunker_cost_usd']:,.0f}"),
            ("  (fuel consumed)",         f"  {result['bunker_consumed_mt']:.0f} mt"),
            ("— Port Costs",              f"-${result['port_cost_usd']:,.0f}"),
            ("— Canal Tolls",             f"-${result['canal_tolls_usd']:,.0f}"),
            ("+ Demurrage Revenue",       f"+${result['demurrage_revenue']:,.0f}"),
            ("= Net Voyage Revenue",      f"${result['net_voyage_revenue']:,.0f}"),
            ("Voyage Duration",           f"{result['total_voyage_days']:.1f} days"),
            ("  Sea (laden)",             f"  {result['sea_days_laden']:.1f} days"),
            ("  Sea (ballast)",           f"  {result['sea_days_ballast']:.1f} days"),
            ("  Port time",               f"  {result['port_days']:.1f} days"),
            ("TCE (USD/day)",             f"${result['tce_usd_day']:,.0f}"),
            ("Vessel OPEX (USD/day)",     f"${result['opex_usd_day']:,.0f}"),
            ("Daily P&L vs OPEX",         f"${result['daily_margin_usd']:,.0f}"),
        ]
        return pd.DataFrame(rows, columns=["Item", "Value"])
