# Templates and presets

Each category has a **folder template** (levels separated by `/`, relative to the destination root)
and a **filename template** (no `/`). The original extension is always kept: if the filename
template does not end in `{ext}`/`{extension}`, the extension is appended.

## Syntax

| Syntax | Meaning |
|---|---|
| `{token}` | value of the token |
| `{token:02}` | zero-padded number (`{track:02}` → `07`) |
| `{a\|b}` | first token that has a value (`{album_artist\|artist}`) |
| `[ … ]` | optional section: shown only when **every** token inside has a value |
| `[[` `]]` | literal `[` and `]` |

**Missing values disappear cleanly.** After rendering, empty brackets, doubled separators and
dangling separators are removed: `{year} - {album}` without a year gives `Album`, not `- Album`.
Separators inside values are left alone (`Jay-Z`, `R.E.M.`, `Wait...`).

**Required vs optional folders.** A folder level made only of optional sections (e.g.
`[Disc {disc:02}]`) disappears when it has no value. A level with a *required* token that has no value
is never silently dropped — the file goes to the review queue ("Missing information needed by the
template: album_artist") so the hierarchy is never flattened by accident.

Every generated name is then sanitized for the destination (see the preview's "Name adjusted" notes):
characters Windows forbids (`<>:"/\|?*`), control characters, trailing spaces and periods, reserved
device names (`CON`, `NUL`, `COM1`…), Unicode normalization (NFC by default), ASCII transliteration on
drivers that reject non-ASCII names, and name/path length limits. With the default **portable**
preset these rules apply everywhere so the library stays valid on any OS; **native** applies only
what the destination itself requires.

## Tokens

**Music** — `album_artist`, `artist`, `album`, `year`, `disc`, `disc_number`, `disc_total`, `track`,
`track_total`, `title`, `genre`, `format` (FLAC, MP3, AAC, ALAC, Opus…), `release`, `original_name`, `ext`

* `disc` has a value **only for multi-disc albums** (several disc numbers present, or a disc total > 1),
  so `[{disc}-]` and `[Disc {disc:02}]` never add disc prefixes or folders to single-disc albums.
  `disc_number` is the raw tag.
* In folder templates `{artist}` is the album-level artist: the majority primary artist (featured
  artists removed), or the compilation name for compilations — so Artist → Album mode never scatters
  an album. In filename templates `{artist}` is the track's own artist including "feat." credits.
* `{album_artist}` for compilations is the configured compilation name (default *Various Artists*).
* `title` falls back to the original filename when the title tag is missing (provenance says so).

**TV** — `series`, `series_year`, `season`, `episode`, `episode_code`, `episode_title`, `absolute`,
`air_date`, `resolution`, `source`, `codec`, `original_name`, `ext`

* `episode_code` is `S01E02`, `S01E01-E03` (contiguous multi-episode), `S01E01E03` (non-contiguous),
  `2026-10-10` (daily shows; season = year) or `123` (absolute numbering, only when TV numbering is
  set to *absolute*). Specials (`S00Exx`) go to `Season 00`; `season` 0 is a value, not "missing".

**Movies** — `title`, `year`, `edition`, `resolution`, `source`, `codec`, `original_name`, `ext`
(multi-part files `cd1`/`part2` get ` - part1`, ` - part2`).

**Documents** — `project` (the top-level folder under the source), `subpath`, `name`, `original_name`, `ext`

**Photos** — `year`, `month`, `day` (EXIF capture date), `camera_make`, `camera_model`, `name`, `ext`

**Other** — `name`, `type` (extension in capitals), `original_name`, `ext`

## Presets

| Category | Preset | Result |
|---|---|---|
| Music | `album_artist/album` (default) | `Album Artist/Album/01 - Title.flac` |
| Music | `artist/album` | `Artist/Album/01 - Title.flac` |
| Music | `album_artist/year-album` | `Album Artist/2013 - Album/01 - Title.flac` |
| Music | `album_artist/album/disc` | `Album Artist/Album/Disc 01/01 - Title.flac` (disc folder only for multi-disc) |
| TV | `series/season` (default) | `Series/Season 01/Series - S01E01 - Episode Title.mkv` |
| TV | `series_year/season` | `Series (2005)/Season 01/Series - S01E01.mkv` |
| TV | `series/season/episode` | `Series/Season 01/Series - S01E01 - Title/Series - S01E01 - Title.mkv` |
| Movies | `title_year` | `Movie Title (Year)/Movie Title (Year).mkv` |
| Documents | `project` | `Project/Filename.pdf` |
| Photos | `year/year-month` | `2024/2024-07/IMG_0412.jpg` |
| Other | `flat` | `Filename.ext` |

Each category also has a **mode**: *sort into folders and rename* (both templates), *sort only*
(folder template, original filename), or *rename in place* (filename template, folder unchanged).

## Companion files

| Files | Behaviour |
|---|---|
| Cover art, booklets (`.pdf`), `.cue`, `.log`, `.m3u`, `.txt` in an album folder | Move with the album when every track of that folder goes to the same album folder; otherwise wait for review |
| Lyrics `track.lrc` | Renamed with their track (`01 - Title.lrc`) |
| Cue sheets / playlists referring to renamed tracks | Review by default; with *Update references* they are rewritten (original kept in the journal for undo). A single-file album image with a multi-track cue keeps its filename so the cue stays valid |
| Subtitles, `.nfo`, `-thumb.jpg`, alternate audio (`.mka`, `.ac3`…) sharing the video's name | Renamed with the video; the suffix after the stem is preserved: `.en.srt`, `.en.forced.srt`, `.pt-BR.sdh.srt`, `-thumb.jpg` |
| Subtitles in a `Subs/` folder or matched by episode code | Attached to their episode, keeping language/accessibility tags |
| `poster.jpg`, `fanart.jpg`, `tvshow.nfo`, `season01-poster.jpg` | Move to the series/movie folder when the folder holds one show/movie |

If a companion cannot move (for example its name collides), its video is held back too, so they
never get separated.

## Examples

```text
{album_artist}/[{year} - ]{album}      [{disc}-][{track:02} - ]{title}
  Daft Punk/2013 - Random Access Memories/08 - Get Lucky.flac
  missing year and track:  Daft Punk/Random Access Memories/Get Lucky.flac

{series}[ ({series_year})]/[Season {season:02}]      {series} - {episode_code}[ - {episode_title}]
  The Expanse (2015)/Season 01/The Expanse - S01E02 - The Big Empty.mkv

{title} ({year})      {title} ({year})[ - {edition}][ - {resolution}]
  Arrival (2016)/Arrival (2016) - 2160p.mkv
```

Try a template without touching any files:

```bash
curator template music --folder "{album_artist}/[{year} - ]{album}" --filename "[{disc}-][{track:02} - ]{title}"
```
