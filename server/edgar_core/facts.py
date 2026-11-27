"""Reported financial figures, read from the SEC's structured (XBRL) data.

Companies tag every number in their financial statements. The SEC publishes
those tagged numbers as JSON, so a figure such as revenue can be looked up
directly, with the filing it came from, instead of being read out of a
document.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from .client import EdgarClient, EdgarError, EdgarHTTPError
from .companies import format_cik
from .dates import check_date, on_or_before

CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{ns}/{tag}.json"  # ns: us-gaap, or dei for cover-page items

# Plain names for common figures. Each maps to the official tag(s) companies
# use for it, most common first. Companies differ, and change tags over time,
# so every tag in the list is checked and the results are combined.
CONCEPTS: Dict[str, List[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps_basic": ["EarningsPerShareBasic"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "rd_expense": ["ResearchAndDevelopmentExpense"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "stockholders_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    # Everyday statement lines a ratio needs (added 29 Oct after the agent had to hunt tables for inventory and COGS).
    "cost_of_goods_sold": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold", "CostOfServices"],
    "inventory": ["InventoryNet", "InventoryFinishedGoodsNetOfReserves"],
    "accounts_receivable": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"],
    "accounts_payable": ["AccountsPayableCurrent", "AccountsPayableAndAccruedLiabilitiesCurrent"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "interest_expense": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"],
    "income_tax_expense": ["IncomeTaxExpenseBenefit"],
    "pretax_income": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "depreciation_amortization": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "DepreciationAmortizationAndAccretionNet"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "dividends_paid": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
    "share_repurchases": ["PaymentsForRepurchaseOfCommonStock"],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "shares_outstanding": ["CommonStockSharesOutstanding", "dei:EntityCommonStockSharesOutstanding"],
    "cover_page_shares": ["dei:EntityCommonStockSharesOutstanding"],
    "sga_expense": ["SellingGeneralAndAdministrativeExpense"],
    "operating_lease_cost": ["OperatingLeaseCost"],
    "investing_cash_flow": ["NetCashProvidedByUsedInInvestingActivities"],
    "financing_cash_flow": ["NetCashProvidedByUsedInFinancingActivities"],
    "long_term_debt": ["LongTermDebt", "LongTermDebtNoncurrent"],
    # Balance-sheet items an enterprise-value build needs (31 Oct: lease-adjusted EV questions)
    "short_term_debt": ["DebtCurrent", "ShortTermBorrowings", "LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
    "long_term_debt_noncurrent": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndFinanceLeaseObligationsNoncurrent"],
    "operating_lease_liability_current": ["OperatingLeaseLiabilityCurrent"],
    "operating_lease_liability_noncurrent": ["OperatingLeaseLiabilityNoncurrent"],
    "finance_lease_liability_current": ["FinanceLeaseLiabilityCurrent"],
    "finance_lease_liability_noncurrent": ["FinanceLeaseLiabilityNoncurrent"],
    "short_term_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "preferred_stock": ["PreferredStockValue", "TemporaryEquityCarryingAmountAttributableToParent"],
    "noncontrolling_interest": ["MinorityInterest"],
}
_ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
_QUARTERLY_FORMS = {"10-Q", "10-Q/A"}
_ANNUAL_DAYS = (340, 380)  # a fiscal year, allowing for 52- and 53-week years
_QUARTER_DAYS = (77, 119)  # 12- to 17-week quarters (Costco's fourth quarter is 16 or 17 weeks)


class ConceptNotFoundError(EdgarError):
    """The company has not reported this figure under any tag we know."""


@dataclass(frozen=True)
class FinancialFact:
    concept: str  # what was asked for, for example "revenue"
    tag: str  # the official tag the number was reported under
    value: float
    unit: str  # "USD", "USD/shares", "shares" ...
    period_start: Optional[str]  # None for a balance-sheet figure (a single date)
    period_end: str
    form: str  # the form it was reported in, for example "10-K"
    filed: str
    accession_number: str  # the filing to cite
    fiscal_year: Optional[int] = None  # the company's OWN label for the period's fiscal year, from the filing's XBRL
    # (DocumentFiscalYearFocus): lululemon's year ending 2025-02-02 is "fiscal 2024", Walmart's year ending
    # 2025-01-31 is "fiscal 2025". Match a question's "FY2024" to this, not to the calendar.

    def to_dict(self) -> dict:
        return asdict(self)


def list_concepts() -> List[str]:
    """Return the plain concept names get_financial_fact() understands."""
    return list(CONCEPTS)


def get_financial_fact(
    cik,
    concept: str,
    period: str = "annual",
    limit: int = 5,
    as_of: Optional[str] = None,
    client: Optional[EdgarClient] = None,
    basis: str = "latest",
) -> List[FinancialFact]:
    """Return a company's reported values for one figure, newest period first.

    `concept` is a plain name from list_concepts(), such as "revenue" or
    "net_income", or an exact us-gaap tag such as "InventoryNet".

    `period` is "annual" (full fiscal years) or "quarterly" (three-month
    periods; note that companies do not report the fourth quarter on its own).
    For balance-sheet figures such as total_assets, "annual" gives the value
    at each fiscal year end and "quarterly" at each quarter end.

    When the same period was reported in more than one filing, the most
    recently filed value is returned, so restatements are picked up
    (`basis="latest"`); `basis="original"` returns the value as first
    reported instead (the earliest filing that carries the period).

    Values filed after `as_of` (YYYY-MM-DD) are ignored, so the answer is
    what was known on that date.
    """
    if period not in ("annual", "quarterly"):
        raise ValueError('period must be "annual" or "quarterly"')
    if basis not in ("latest", "original"):
        raise ValueError('basis must be "latest" or "original"')
    if limit < 1:
        raise ValueError("limit must be at least 1")
    name = concept.strip()
    if not name:
        raise ValueError("concept is empty")
    key = name.lower().replace(" ", "_").replace("-", "_")
    tags = CONCEPTS.get(key, [name])
    label = key if key in CONCEPTS else name

    as_of = check_date(as_of, "as_of")
    cik10 = format_cik(cik)
    client = client or EdgarClient()

    # period -> (tag priority, filed date, fact). Lower priority number wins,
    # then the later filing.
    chosen: Dict[Tuple[Optional[str], str], Tuple[int, str, FinancialFact]] = {}
    filing_focus: Dict[str, Tuple[int, str]] = {}  # accession -> (fiscal year focus of the filing, its latest period end)
    for priority, tag in enumerate(tags):
        ns, _, bare = tag.rpartition(":")
        try:
            data = client.get_json(CONCEPT_URL.format(cik=cik10, ns=ns or "us-gaap", tag=bare))
        except EdgarHTTPError as error:
            if error.status == 404:  # this company never used this tag
                continue
            raise
        units = data.get("units", {})
        if not units:
            continue
        unit = "USD" if "USD" in units else next(iter(units))
        for entry in units[unit]:
            accn, fy, end = entry.get("accn"), entry.get("fy"), entry.get("end")
            if accn and isinstance(fy, int) and end:
                known = filing_focus.get(accn)
                if known is None or end > known[1]:
                    filing_focus[accn] = (fy, end)
            if not _matches(entry, period):
                continue
            if not on_or_before(entry.get("filed", ""), as_of):
                continue
            period_key = (entry.get("start"), entry["end"])
            filed = entry.get("filed", "")
            current = chosen.get(period_key)
            if current and (current[0] < priority or (current[0] == priority and (
                    current[1] >= filed if basis == "latest" else current[1] <= filed))):
                continue
            chosen[period_key] = (
                priority,
                filed,
                FinancialFact(
                    concept=label,
                    tag=bare,
                    value=entry["val"],
                    unit=unit,
                    period_start=entry.get("start"),
                    period_end=entry["end"],
                    form=entry.get("form", ""),
                    filed=filed,
                    accession_number=entry.get("accn", ""),
                ),
            )

    if not chosen:
        raise ConceptNotFoundError(
            f"No {period} data for {concept!r} (CIK {cik10}). "
            f"Known concept names: {', '.join(CONCEPTS)}. "
            "An exact us-gaap tag can also be used."
        )
    facts = [_with_fiscal_year(fact, filing_focus) for _, _, fact in chosen.values()]
    facts.sort(key=lambda fact: fact.period_end, reverse=True)
    return facts[:limit]


def _with_fiscal_year(fact: FinancialFact, filing_focus: Dict[str, Tuple[int, str]]) -> FinancialFact:
    """The SEC's "fy" on a reported value is the fiscal-year focus of the FILING, the same for the
    current year and the prior-year comparatives it contains. The period's own label is the
    filing's focus minus the number of years the period precedes the filing's latest period."""
    focus = filing_focus.get(fact.accession_number)
    if not focus:
        return fact
    fy, latest_end = focus
    try:
        # Count fiscal years, not calendar years: a 52/53-week year can end on 2024-12-28 and the
        # next on 2026-01-03, one year apart though the calendar years differ by two.
        years_back = round((date.fromisoformat(latest_end) - date.fromisoformat(fact.period_end)).days / 365.25)
    except (TypeError, ValueError):
        return fact
    return FinancialFact(**{**asdict(fact), "fiscal_year": fy - years_back})


def _matches(entry: dict, period: str) -> bool:
    """Is this reported value for the kind of period that was asked for?"""
    form = entry.get("form", "")
    start, end = entry.get("start"), entry.get("end")
    if not end or "val" not in entry:
        return False
    if start is None:  # a balance-sheet figure: a value on a single date
        if period == "annual":
            return form in _ANNUAL_FORMS
        return form in _ANNUAL_FORMS or form in _QUARTERLY_FORMS
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    low, high = _ANNUAL_DAYS if period == "annual" else _QUARTER_DAYS
    if not low <= days <= high:
        return False
    # The fiscal-year number the SEC attaches ("fy") is the year of the
    # *filing*, not of the period, so it is deliberately not used here.
    return form in _ANNUAL_FORMS if period == "annual" else True
