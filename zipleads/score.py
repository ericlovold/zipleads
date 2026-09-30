"""Priority score from the profile's weights. Higher means call first."""

from __future__ import annotations

from zipleads.config import Profile

FLAG_DEPRIORITIZED = "flag:deprioritized"
FLAG_BOOST = "flag:boost"


def score_lead(
    profile: Profile,
    sources: str,
    signals: str,
    distinct_addresses: int,
    phone: str,
    contact_email: str,
    value: float = 0.0,
) -> int:
    source_list = [s for s in sources.split(",") if s]
    signal_list = [s for s in signals.split(",") if s]
    real_signals = [s for s in signal_list if not s.startswith("flag:")]

    def weight(source: str) -> int:
        if source in profile.source_weights:
            return profile.source_weights[source]
        if source.endswith("_permits"):
            return profile.source_weights.get("permits", 10)
        return 10

    score = max((weight(s) for s in source_list), default=0)
    if len(source_list) > 1:
        score += 10
    if distinct_addresses >= 2:
        score += profile.multi_site_bonus
    if phone:
        score += profile.phone_bonus
    if contact_email:
        score += profile.contact_bonus
    if source_list == ["google_news"]:
        score += profile.news_only_penalty
    if real_signals and all(s == "news:unparsed" for s in real_signals):
        score += profile.unparsed_penalty
    if any(s.startswith(FLAG_DEPRIORITIZED) for s in signal_list):
        score += profile.deprioritize_penalty
    if any(s.startswith(FLAG_BOOST) for s in signal_list):
        score += profile.boost_bonus
    reached = [bonus for threshold, bonus in profile.value_tiers if value >= threshold]
    if reached:
        score += max(reached)  # permit valuation as a proxy for build-out size and fiber odds
    return score
