import unicodedata

import pytest

from datacurator.models import TemplateError
from datacurator.naming.sanitize import NamingRules, equivalence_key, sanitize_component
from datacurator.naming.template import Template, clean_component
from datacurator.storage.capabilities import Capabilities


def test_optional_sections_and_padding():
    t = Template("[{disc}-][{track:02} - ]{title}", "filename", "music")
    assert t.render_filename({"track": 3, "title": "Song"}, ".flac") == ("03 - Song.flac", [])
    assert t.render_filename({"disc": 2, "track": 3, "title": "Song"}, ".flac")[0] == "2-03 - Song.flac"
    assert t.render_filename({"title": "Song"}, ".flac")[0] == "Song.flac"


def test_missing_token_punctuation_disappears():
    t = Template("{album_artist}/{year} - {album}", "folder", "music")
    parts, missing = t.render_folder({"album_artist": "A", "album": "B"})
    assert parts == ["A", "B"] and missing == ["year"]
    t2 = Template("{album_artist}/[{year} - ]{album}", "folder", "music")
    assert t2.render_folder({"album_artist": "A", "album": "B"}) == (["A", "B"], [])
    assert t2.render_folder({"album_artist": "A", "album": "B", "year": 1999}) == (["A", "1999 - B"], [])


def test_required_folder_token_missing_is_reported_not_flattened():
    t = Template("{album_artist}/{album}", "folder", "music")
    parts, missing = t.render_folder({"album": "B"})
    assert missing == ["album_artist"]


def test_optional_folder_component_vanishes():
    t = Template("{album_artist}/{album}/[Disc {disc:02}]", "folder", "music")
    assert t.render_folder({"album_artist": "A", "album": "B"}) == (["A", "B"], [])
    assert t.render_folder({"album_artist": "A", "album": "B", "disc": 2}) == (["A", "B", "Disc 02"], [])


def test_season_zero_is_a_value():
    t = Template("{series}/[Season {season:02}]", "folder", "tv")
    assert t.render_folder({"series": "S", "season": 0}) == (["S", "Season 00"], [])


def test_fallback_chain_and_extension_preserved():
    t = Template("{album_artist|artist} - {title}{extension}", "filename", "music")
    assert t.render_filename({"artist": "X", "title": "Y"}, ".m4a")[0] == "X - Y.m4a"


def test_unknown_token_rejected():
    with pytest.raises(TemplateError):
        Template("{bogus}", "folder", "music")
    with pytest.raises(TemplateError):
        Template("[{title}", "filename", "music")


def test_clean_component_keeps_meaningful_punctuation():
    assert clean_component("R.E.M.") == "R.E.M."
    assert clean_component("Jay-Z") == "Jay-Z"
    assert clean_component("Wait...") == "Wait..."
    assert clean_component(" - Album") == "Album"
    assert clean_component("Artist - - Album") == "Artist - Album"
    assert clean_component("Movie ()") == "Movie"


def caps(**kw):
    c = Capabilities(path="/x")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_reserved_names_and_prohibited_chars_portable():
    r = NamingRules()
    s, why = sanitize_component("CON", r, caps(), True, ".mp3")
    assert s == "CON_" and any("reserved" in w for w in why)
    s, why = sanitize_component("AC/DC: Live?", r, caps(), False)
    assert s == "AC-DC - Live" and why
    s, why = sanitize_component("Trailing. ", r, caps(), False)
    assert s == "Trailing"


def test_native_preset_on_ext4_keeps_colon():
    r = NamingRules(preset="native")
    s, why = sanitize_component("A: B", r, caps(windows_naming=False), False)
    assert s == "A: B" and not why


def test_unicode_normalized_and_length_truncated_at_codepoint():
    r = NamingRules()
    nfd = unicodedata.normalize("NFD", "Beyoncé")
    s, why = sanitize_component(nfd, r, caps(), False)
    assert s == unicodedata.normalize("NFC", "Beyoncé") and "Unicode normalized to NFC" in why
    long = "é" * 300
    s, why = sanitize_component(long, r, caps(max_name_bytes=255), True, ".flac")
    assert len((s + ".flac").encode()) <= 255 and s.endswith("é")


def test_equivalence_key_case_and_unicode():
    r = NamingRules()
    a = equivalence_key("/m/Beyoncé/Song.flac", caps(), r)
    b = equivalence_key(unicodedata.normalize("NFD", "/m/BEYONCÉ/song.flac"), caps(), r)
    assert a == b
    native = NamingRules(preset="native")
    assert equivalence_key("/m/A", caps(case_sensitive=True), native) != equivalence_key("/m/a", caps(case_sensitive=True), native)
