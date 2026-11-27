"""edgar_core: a small, dependency-free client for the SEC EDGAR APIs."""

from .client import EdgarClient, EdgarConfigError, EdgarError, EdgarHTTPError
from .companies import TickerNotFoundError, ticker_to_cik
from .documents import (
    DocumentNotFoundError,
    FilingDocument,
    find_document,
    find_in_document,
    get_exhibit,
    list_documents,
    read_document,
)
from .entities import EntityMatch, EntityNotFoundError, EntityProfile, get_entity_profile, resolve_entity
from .facts import ConceptNotFoundError, FinancialFact, get_financial_fact, list_concepts
from .filings import Filing, filing_history, list_filings
from .forms import FormInfo, categories, describe_form, forms_for, list_forms, normalize_form
from .nport import (
    CdsContract,
    CdsExposure,
    NportReport,
    ReportNotFoundError,
    cds_exposure,
    get_portfolio_report,
    latest_portfolio_report,
    parse_nport,
    summarize_cds,
)
from .search import SearchHit, SearchResults, search_filings
from .sections import FilingSection, SectionNotFoundError, get_filing_section, list_sections
from .tables import Table, TableNotFoundError, extract_tables, get_table, list_tables
from .text import html_to_text

__all__ = [
    "FormInfo",
    "categories",
    "describe_form",
    "forms_for",
    "list_forms",
    "normalize_form",
    "CdsContract",
    "CdsExposure",
    "EntityMatch",
    "EntityNotFoundError",
    "EntityProfile",
    "NportReport",
    "ReportNotFoundError",
    "cds_exposure",
    "get_entity_profile",
    "get_portfolio_report",
    "latest_portfolio_report",
    "parse_nport",
    "resolve_entity",
    "summarize_cds",
    "DocumentNotFoundError",
    "FilingDocument",
    "Table",
    "TableNotFoundError",
    "extract_tables",
    "find_document",
    "find_in_document",
    "get_exhibit",
    "get_table",
    "list_documents",
    "list_tables",
    "read_document",
    "SearchHit",
    "SearchResults",
    "search_filings",
    "ConceptNotFoundError",
    "FinancialFact",
    "get_financial_fact",
    "list_concepts",
    "EdgarClient",
    "EdgarConfigError",
    "EdgarError",
    "EdgarHTTPError",
    "Filing",
    "FilingSection",
    "SectionNotFoundError",
    "TickerNotFoundError",
    "get_filing_section",
    "html_to_text",
    "list_filings",
    "filing_history",
    "list_sections",
    "ticker_to_cik",
]
from .metrics import Metric, MetricNotFoundError, categories as metric_categories, describe_metric, metrics_in_text, list_metrics  # noqa: E402,F401
from .prices import PriceBar, PriceError, PriceSeries, price_change, price_history  # noqa: E402,F401
