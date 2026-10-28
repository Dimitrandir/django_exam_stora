"""Session storage for the 3 parallel "open operations" on the delivery /
write-off / scrap add screens -- a warehouse clerk can park a half-entered
delivery on one tab, start another on a different tab, and switch back.
Mirrors STORA.sales.tab_state (same idea, same shape), but each tab's draft
also remembers WHICH kind of operation it is (delivery / write_off /
scrap, see core.utils.build_cashier_operation_state's `type`/`path`), so
tab 2 can hold a write-off while tab 1 holds a delivery.

Replaces the old single `cashier_last_operation` slot for these screens --
with one slot, opening a second delivery (even in another browser tab)
silently overwrote the first one's draft.
"""

DELIVERY_TABS_SESSION_KEY = 'delivery_tabs'
ACTIVE_DELIVERY_TAB_SESSION_KEY = 'active_delivery_tab'
TAB_IDS = ['1', '2', '3']
DEFAULT_TAB = TAB_IDS[0]


def get_active_tab(request) -> str:
    tab = request.session.get(ACTIVE_DELIVERY_TAB_SESSION_KEY, DEFAULT_TAB)
    return tab if tab in TAB_IDS else DEFAULT_TAB


def set_active_tab(request, tab: str) -> None:
    request.session[ACTIVE_DELIVERY_TAB_SESSION_KEY] = tab
    request.session.modified = True


def get_tab_draft(request, tab: str) -> dict | None:
    return request.session.get(DELIVERY_TABS_SESSION_KEY, {}).get(tab)


def set_tab_draft(request, tab: str, draft: dict) -> None:
    tabs = request.session.get(DELIVERY_TABS_SESSION_KEY, {})
    tabs[tab] = draft
    request.session[DELIVERY_TABS_SESSION_KEY] = tabs
    request.session.modified = True


def clear_tab_draft(request, tab: str) -> None:
    tabs = request.session.get(DELIVERY_TABS_SESSION_KEY, {})
    tabs.pop(tab, None)
    request.session[DELIVERY_TABS_SESSION_KEY] = tabs
    request.session.modified = True


def draft_has_items(draft: dict | None) -> bool:
    """A draft only counts as "in use" once it has at least one product row
    -- a header-only draft (supplier picked, grid still empty) is cheap to
    lose and shouldn't pin a tab."""
    if not draft:
        return False
    forms = draft.get('formset_data', {}).get('forms', [])
    return any(row.get('delivery_item') for row in forms)


def resolve_tab_for_page(request, operation_type: str) -> str:
    """Which tab a freshly opened add screen of `operation_type` should
    use (and makes it the active one). Keeps the current active tab when
    it's free or already holds the same kind of operation; otherwise moves
    to the first tab that holds this kind of operation, then the first
    free tab -- so opening e.g. Write-off from the menu while tab 1 holds
    a delivery lands on a free tab instead of overwriting the delivery."""
    active = get_active_tab(request)
    active_draft = get_tab_draft(request, active)
    chosen = active
    if draft_has_items(active_draft) and active_draft.get('type') != operation_type:
        same_type = [t for t in TAB_IDS
                     if draft_has_items(get_tab_draft(request, t))
                     and get_tab_draft(request, t).get('type') == operation_type]
        free = [t for t in TAB_IDS if not draft_has_items(get_tab_draft(request, t))]
        if same_type:
            chosen = same_type[0]
        elif free:
            chosen = free[0]
    if chosen != active:
        set_active_tab(request, chosen)
    return chosen


def tabs_summary(request) -> list[dict]:
    """One row per tab for the tab buttons -- has_items fills the icon,
    is_active highlights, `type` is shown as a hint (title tooltip)."""
    active_tab = get_active_tab(request)
    summary = []
    for tab in TAB_IDS:
        draft = get_tab_draft(request, tab)
        has_items = draft_has_items(draft)
        summary.append({
            'tab': tab,
            'has_items': has_items,
            'is_active': tab == active_tab,
            'type': draft.get('type') if has_items else None,
        })
    return summary
