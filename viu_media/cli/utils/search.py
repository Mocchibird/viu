"""Search functionality."""

import re

from viu_media.core.utils.fuzzy import fuzz
from viu_media.core.utils.normalizer import normalize_title
from viu_media.libs.provider.anime.base import BaseAnimeProvider
from viu_media.libs.provider.anime.params import SearchParams
from viu_media.libs.provider.anime.types import (
    SearchResult,
    SearchResults,
    ProviderName,
)
from viu_media.libs.media_api.types import MediaItem

# "Season 2", "2nd Season", "Part 2" and "II" all name the same sequel, but plain
# edit distance scores "Season 2" closer to ": The Movie" than to "II".
_SEQUEL_MARKER = re.compile(
    r"\b(?:season|part|cour)\s+(\d+)\b|\b(\d+)(?:st|nd|rd|th)\s+(?:season|part|cour)\b"
)
_ROMAN_NUMERALS = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8"}
_FORMAT_BONUS = 5
_YEAR_BONUS = 5
# Right matches score 100+ with their bonuses; a best score this low means the
# search never returned the show at all.
_CONFIDENT_MATCH = 85


def _canonical(title: str) -> str:
    title = _SEQUEL_MARKER.sub(lambda m: m.group(1) or m.group(2), title.lower())
    words = re.sub(r"[^\w\s]", " ", title).split()
    return " ".join(_ROMAN_NUMERALS.get(word, word) for word in words)


def _match_score(
    result: SearchResult, provider: ProviderName, media_item: MediaItem
) -> int:
    provider_title = _canonical(normalize_title(result.title, provider.value))
    names = [media_item.title.english, media_item.title.romaji, *media_item.synonymns]
    score = max(fuzz.ratio(provider_title, _canonical(name)) for name in names if name)

    # Break near-ties with facts the title can't carry, e.g. a movie that shares
    # most of its name with the TV season being looked for.
    if result.media_type and media_item.format:
        provider_format = result.media_type.upper().replace(" ", "_")
        if provider_format == media_item.format.value:
            score += _FORMAT_BONUS
    if media_item.start_date and result.year == str(media_item.start_date.year):
        score += _YEAR_BONUS
    return score


def find_best_match_title(
    provider_results_map: dict[str, SearchResult],
    provider: ProviderName,
    media_item: MediaItem,
) -> str:
    """Find the provider result that best matches the media item.

    Fuzzy matches every title the media item is known by (english, romaji and
    synonyms), with sequel markers made comparable, then prefers results whose
    format and year agree with the media item.

    Parameters:
        provider_results_map (dict[str, SearchResult]): The map of provider results.
        provider (ProviderName): The provider name from the config.
        media_item (MediaItem): The media item to match.

    Returns:
        str: The best match title.
    """
    return max(
        provider_results_map.keys(),
        key=lambda p_title: _match_score(
            provider_results_map[p_title], provider, media_item
        ),
    )


def search_provider(
    provider: BaseAnimeProvider,
    provider_name: ProviderName,
    media_item: MediaItem,
    translation_type: str = "sub",
) -> SearchResults | None:
    """Search the provider by the media's english title, then by its romaji title
    if the english search found nothing that confidently matches.

    Provider search is literal: animepahe finds "Re:ZERO -Starting Life in Another
    World- Season 2 Part 2" by its romaji title but not by its english one.
    """
    found: SearchResults | None = None
    titles = dict.fromkeys(
        t for t in (media_item.title.english, media_item.title.romaji) if t
    )
    for title in titles:
        results = provider.search(
            SearchParams(
                query=normalize_title(title, provider_name.value, True).lower(),
                translation_type=translation_type,  # type: ignore[arg-type]
            )
        )
        if not results or not results.results:
            continue
        if found is None:
            # copy, since providers cache the results they return
            found = results.model_copy(update={"results": list(results.results)})
        else:
            seen = {r.id for r in found.results}
            found.results.extend(r for r in results.results if r.id not in seen)
        best = max(_match_score(r, provider_name, media_item) for r in found.results)
        if best >= _CONFIDENT_MATCH:
            break
    return found
