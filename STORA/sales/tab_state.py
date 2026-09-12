"""Session storage for the 3 parallel sale "baskets" on the POS screen
(sale_add.html) -- a cashier can park an in-progress sale on one tab,
switch to another (empty) tab to ring up a different customer, then switch
back. This is deliberately separate from core.session_service's single
cashier_last_operation slot (shared by deliveries/write-off/scrap, which
only ever need one draft at a time) -- sales needed its own multi-slot
version, so it gets its own small module instead of complicating the
shared one for everybody else.
"""

SALE_TABS_SESSION_KEY = 'sale_tabs'
ACTIVE_SALE_TAB_SESSION_KEY = 'active_sale_tab'
LAST_CHANGE_SESSION_KEY = 'sale_tabs_last_change'
TAB_IDS = ['1', '2', '3']
DEFAULT_TAB = TAB_IDS[0]


def get_active_tab(request) -> str:
    tab = request.session.get(ACTIVE_SALE_TAB_SESSION_KEY, DEFAULT_TAB)
    return tab if tab in TAB_IDS else DEFAULT_TAB


def set_active_tab(request, tab: str) -> None:
    request.session[ACTIVE_SALE_TAB_SESSION_KEY] = tab
    request.session.modified = True


def get_tab_draft(request, tab: str) -> dict | None:
    return request.session.get(SALE_TABS_SESSION_KEY, {}).get(tab)


def set_tab_draft(request, tab: str, draft: dict) -> None:
    tabs = request.session.get(SALE_TABS_SESSION_KEY, {})
    tabs[tab] = draft
    request.session[SALE_TABS_SESSION_KEY] = tabs
    request.session.modified = True


def clear_tab_draft(request, tab: str) -> None:
    tabs = request.session.get(SALE_TABS_SESSION_KEY, {})
    tabs.pop(tab, None)
    request.session[SALE_TABS_SESSION_KEY] = tabs
    request.session.modified = True


def get_last_change(request, tab: str) -> str | None:
    """The Change amount from that tab's most recently completed sale --
    kept on screen (top-right, see sale_add.html) after the cashier
    finishes a sale and the page reloads to a fresh cart, until they start
    ringing up the next customer (see clear_last_change)."""
    return request.session.get(LAST_CHANGE_SESSION_KEY, {}).get(tab)


def set_last_change(request, tab: str, value) -> None:
    amounts = request.session.get(LAST_CHANGE_SESSION_KEY, {})
    # Stored as a string -- the session backend JSON-serializes this dict,
    # which can't handle a Decimal directly.
    amounts[tab] = str(value)
    request.session[LAST_CHANGE_SESSION_KEY] = amounts
    request.session.modified = True


def clear_last_change(request, tab: str) -> None:
    amounts = request.session.get(LAST_CHANGE_SESSION_KEY, {})
    amounts.pop(tab, None)
    request.session[LAST_CHANGE_SESSION_KEY] = amounts
    request.session.modified = True


def tabs_summary(request) -> list[dict]:
    """One row per tab for the 3 basket-icon buttons -- has_items decides
    the filled-vs-empty basket icon, is_active decides the highlight."""
    tabs = request.session.get(SALE_TABS_SESSION_KEY, {})
    active_tab = get_active_tab(request)
    summary = []
    for tab in TAB_IDS:
        draft = tabs.get(tab)
        has_items = bool(draft and draft.get('formset_data', {}).get('forms'))
        summary.append({'tab': tab, 'has_items': has_items, 'is_active': tab == active_tab})
    return summary
