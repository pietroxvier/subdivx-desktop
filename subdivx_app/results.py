"""Agrupa publicações por obra antes de apresentar versões de legenda."""
from dataclasses import dataclass, field
import re

from .core import SearchSpec, Subtitle, YEAR, episode_of, identity_title, plain


@dataclass
class TitleGroup:
    key: tuple
    title: str
    year: int | None
    episode: tuple
    items: list[Subtitle] = field(default_factory=list)

    @property
    def label(self):
        text = self.title + (f' ({self.year})' if self.year else '')
        season, episode = self.episode
        if season is not None:
            text += f' · S{season:02d}'
        if episode is not None:
            text += f'E{episode:02d}'
        return text


def group_titles(items: list[Subtitle], spec: SearchSpec) -> list[TitleGroup]:
    groups = {}
    seen = set()
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        title = plain(item.title)
        year_match = YEAR.search(title)
        year = int(year_match[0]) if year_match else None
        # O ano delimita o nome na maioria das publicações; não descarta o
        # título de filmes cujo próprio nome é um número, como 1917.
        split = re.search(r'\s*[([](?:19|20)\d{2}[)\]]', title)
        if split and split.start() > 0:
            title = title[:split.start()]
        title = re.split(r'\s+(?:aka|a\.k\.a\.)\s+', title, flags=re.I)[0]
        title = re.sub(r'\s*[-–|]?\s*\b(?:4k|2160p|1080p|720p|remastered|extended|director.s cut)\b.*$', '', title, flags=re.I)
        title = title.strip(' ._-–') or item.title
        episode = episode_of(item.title) if spec.kind == 'Série' else (None, None)
        key = (identity_title(title), year, episode)
        if key not in groups:
            groups[key] = TitleGroup(key, title, year, episode)
        groups[key].items.append(item)
    for group in groups.values():
        group.items.sort(key=lambda item: (item.score, item.downloads), reverse=True)
    # Obras em sequência cronológica; versões de uma mesma obra por adequação.
    return sorted(groups.values(), key=lambda group: (
        group.year is None, group.year or 9999, group.key[0],
        group.episode[0] if group.episode[0] is not None else -1,
        group.episode[1] if group.episode[1] is not None else -1))
