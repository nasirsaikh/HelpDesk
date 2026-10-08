"""Static agent capabilities. Company configuration can narrow these, never add executable code."""

DOMAINS = {
    "CLAIMS": "Claims",
    "POLICY": "Policy",
    "FINANCE": "Finance",
    "TICKETING": "Ticketing",
}
DOMAIN_RESOURCES = {
    "CLAIMS": "ticket",
    "POLICY": "policy",
    "FINANCE": "report",
    "TICKETING": "ticket",
}
DOMAIN_FEATURES = {
    "CLAIMS": ("claims", "tickets"),
    "POLICY": ("policies",),
    "FINANCE": ("analytics", "tpa"),
    "TICKETING": ("tickets",),
}
DOMAIN_TOOLS = {
    "CLAIMS": {
        "search_claims",
        "get_claim",
        "get_policy",
        "search_knowledge",
        "propose_ticket_comment",
    },
    "POLICY": {"search_policies", "get_policy", "search_members", "search_knowledge"},
    "FINANCE": {"premium_summary", "calculate_total", "search_knowledge"},
    "TICKETING": {"search_tickets", "get_ticket", "search_knowledge", "propose_ticket_comment"},
}
ROUTE_WORDS = {
    "CLAIMS": ("claim", "claims", "clm", "مطالبة", "مطالبات"),
    "POLICY": (
        "policy",
        "policies",
        "coverage",
        "endorsement",
        "enrollment",
        "member",
        "members",
        "pol",
        "وثيقة",
        "تغطية",
    ),
    "FINANCE": (
        "finance",
        "financial",
        "premium",
        "premiums",
        "payment",
        "payments",
        "reconcile",
        "reconciliation",
        "invoice",
        "refund",
        "مالية",
        "دفع",
    ),
    "TICKETING": ("ticket", "tickets", "helpdesk", "assignment", "reply", "تذكرة"),
}


def default_agents():
    prompts = {
        "CLAIMS": "Assist with authorized claim requests. Identify missing information, summarize recorded facts, and draft clear updates. Do not decide coverage or approve settlement.",
        "POLICY": "Assist with authorized policies, benefit plans and members. Explain recorded terms and missing information. Do not invent coverage or change enrollment.",
        "FINANCE": "Analyze recorded endorsement premium impacts and calculate totals using tools. This application has no payment ledger: never describe premium impacts as payments, refunds issued or settlement balances. State that limitation when asked about payments.",
        "TICKETING": "Assist with authorized support tickets. Summarize history, suggest next steps and draft helpful comments. A proposed comment requires the user's review before it is posted.",
    }
    return [
        {
            "code": code,
            "name": label + " agent",
            "domain": code,
            "system_prompt": prompts[code],
            "knowledge_categories": [label],
            "allowed_tools": sorted(DOMAIN_TOOLS[code]),
            "allowed_roles": ["ADMIN", "MANAGER", "AUDITOR"] if code == "FINANCE" else [],
            "is_default": True,
        }
        for code, label in DOMAINS.items()
    ]
