"""フロントエンド資産の回帰ガード。

**ファイルを読んで文字列を検査するだけの安いテストだが、Phase 5 で潰した
バグに直接効く。** 体感バグ A / F はどちらも「JS の書き方」が原因で、
Python 側のテストでは一切captureできない種類のものだった。

ビルド工程が無い（バンドラもトランスパイラも入れない方針）ため、
import パスや DOM の id を 1 文字間違えると **画面が真っ白になるだけで
どこにもエラーが残らない**。型検査の代わりにここで受け止める。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ir_remocon.app import config

INDEX_HTML = config.TEMPLATES_DIR / "index.html"
JS_DIR = config.STATIC_DIR / "js"

# 禁止語の検査は **コードだけ** を対象にする。禁止した理由はコメントに書いて
# あるので、素朴に全文検索すると自分の説明文に引っかかる (実際に引っかかった)。
# コメントで説明できなくなると、次に触る人が理由を知らないまま同じ書き方に
# 戻してしまうため、検査側を直すのが正しい。
_JS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
#: ``http://`` を巻き込まないよう、直前が ``:`` の場合は行コメントとみなさない。
_JS_LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _js_files() -> list[Path]:
    return sorted(JS_DIR.glob("*.js"))


def _js_sources() -> list[tuple[str, str]]:
    """コメントを除いた JS のソース (ファイル名, 中身)。"""
    return [
        (path.name, _strip_js_comments(path.read_text(encoding="utf-8")))
        for path in _js_files()
    ]


def _strip_js_comments(source: str) -> str:
    return _JS_LINE_COMMENT.sub("", _JS_BLOCK_COMMENT.sub("", source))


def _html_source() -> str:
    """コメントを除いた index.html。"""
    return _HTML_COMMENT.sub("", INDEX_HTML.read_text(encoding="utf-8"))


# -----------------------------------------------------------------------------
# 配信されていること
# -----------------------------------------------------------------------------
def test_root_serves_ui(client):
    """``/`` が UI を返すこと。

    Phase 4 まではここが JSON スタブで、旧 UI は ``esp32_ip`` 前提だったため
    意図的に配信していなかった。Phase 5 で反転した。
    """
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<title>Smart IR Remote</title>" in response.text


def test_static_assets_are_served(client):
    for path in ("/static/style.css", "/static/js/app.js"):
        assert client.get(path).status_code == 200, path


def test_index_loads_app_as_module(client):
    """ES modules で読み込んでいること。``type="module"`` が無いと import が全滅する。"""
    assert '<script type="module" src="/static/js/app.js">' in client.get("/").text


# -----------------------------------------------------------------------------
# ★ 不具合 A の回帰ガード
# -----------------------------------------------------------------------------
def test_index_has_no_required_attribute():
    """★ 非表示の必須コントロールがブラウザの submit を握り潰す事故の再発防止。

    旧 ``index.html`` は 4 箇所 (60/65/66/77 行) に ``required`` を持ち、
    ``display:none`` で隠れた空の select が制約検証に引っかかっていた。
    submit イベント自体が発火しないので JS のエラー表示すら出ず、
    「予約ボタンが無反応」という体感バグの主犯になっていた。

    検証は JS 側に一本化してあるので、ここに ``required`` は 1 つも要らない。
    """
    source = _html_source()

    assert re.search(r"\brequired\b", source) is None, (
        "index.html に required が復活しています。非表示のフォーム部品に付くと"
        " submit が黙って握り潰されます (不具合 A)。検証は JS 側で行ってください"
    )


def test_forms_are_novalidate():
    """ブラウザの制約検証そのものを止めてあること (不具合 A の二重の防御)。"""
    source = _html_source()
    forms = re.findall(r"<form\b[^>]*>", source)

    assert forms, "フォームが 1 つも無い"
    for form in forms:
        assert "novalidate" in form, f"novalidate が無いフォームがあります: {form}"


def test_hidden_fieldsets_are_also_disabled():
    """非表示のモード用 fieldset は ``hidden`` と ``disabled`` を両方持つこと。

    ``disabled`` な fieldset の子孫は制約検証の対象外かつ送信対象外になる、
    というのが HTML 仕様上の正攻法。``hidden`` だけだと不具合 A がそのまま再発する。
    """
    source = _html_source()

    for tag in re.findall(r"<fieldset\b[^>]*>", source):
        if "hidden" in tag:
            assert "disabled" in tag, f"hidden なのに disabled でない fieldset: {tag}"


# -----------------------------------------------------------------------------
# ★ 不具合 F の回帰ガード
# -----------------------------------------------------------------------------
def test_no_toisostring_in_javascript():
    """★ 日付の既定値を UTC で作る事故の再発防止。

    ``toISOString()`` は UTC を返すので、JST では 00:00〜08:59 の間だけ
    前日の日付になる。旧実装は日付だけこれで作り、時刻は現地時刻の
    ``toTimeString()`` で作っていたため両者が食い違っていた。
    現地時刻の組み立ては ``format.js`` の ``localDateValue()`` を使うこと。
    """
    for name, source in _js_sources():
        assert "toISOString(" not in source, (
            f"{name} に toISOString() があります。日付を UTC で組み立てると"
            " 朝 9 時前に前日の日付になります (不具合 F)。"
            " format.js の localDateValue() / localTimeValue() を使ってください"
        )


# -----------------------------------------------------------------------------
# ★ XSS と旧 API 契約の残骸
# -----------------------------------------------------------------------------
def test_no_innerhtml_in_javascript():
    """★ 信号名は未エスケープで HTML に入りうる (ユーザが自由に命名できる)。

    旧実装は 3 箇所で ``innerHTML`` にテンプレート文字列を代入していた。
    ``dom.js`` の ``el()`` / ``render()`` は必ず ``textContent`` /
    ``setAttribute`` を通るので、そもそも解釈される経路が存在しない。
    """
    for name, source in _js_sources():
        assert "innerHTML" not in source, (
            f"{name} に innerHTML があります。信号名はユーザ入力なので"
            " dom.js の el() / render() を使ってください"
        )


def test_no_legacy_esp32_ip_in_javascript():
    """旧 API 契約 (``esp32_ip``) の残骸が無いこと。

    新 API は ``extra='forbid'`` なので、送ると 422 になる。送信先は
    ``device_id`` (省略時は既定機器)。
    """
    for name, source in _js_sources():
        assert "esp32_ip" not in source, f"{name} に esp32_ip が残っています"


def test_signal_names_go_through_encodeuricomponent():
    """パスに埋める値は必ずエンコードすること。

    旧実装は ``api/send/${name}`` を素で埋めていたので、名前に ``/`` や ``#``
    が入るとリクエスト先が壊れていた。``api.js`` の ``seg()`` を使う。
    """
    assert "encodeURIComponent" in dict(_js_sources())["api.js"]

    for name, source in _js_sources():
        for path in re.findall(r"`(/api/[^`]*\$\{[^`]*)`", source):
            assert "seg(" in path, f"{name}: エンコードせずに埋めています -> {path}"


# -----------------------------------------------------------------------------
# ビルド工程が無いぶんの安全網
# -----------------------------------------------------------------------------
def test_html_references_existing_static_files():
    """``/static/...`` の参照先が実在すること (パスのタイプミス検出)。"""
    source = _html_source()
    refs = re.findall(r'(?:href|src)="(/static/[^"]+)"', source)

    assert refs, "static への参照が 1 つも無い"
    for ref in refs:
        target = config.STATIC_DIR / ref[len("/static/"):]
        assert target.is_file(), f"{ref} が存在しません"


def test_js_imports_resolve():
    """相対 import の解決先が実在すること。

    バンドラが無いので、綴りを間違えるとブラウザのコンソールに出るだけで
    **画面は真っ白になる**。CI で気づけるようにしておく。
    """
    for path in _js_files():
        source = _strip_js_comments(path.read_text(encoding="utf-8"))
        for target in re.findall(r"""from\s+['"](\.[^'"]+)['"]""", source):
            assert (path.parent / target).resolve().is_file(), (
                f"{path.name} の import 先が見つかりません: {target}"
            )


_EXPORT_DECL = re.compile(
    r"export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z0-9_$]+)"
)
_EXPORT_LIST = re.compile(r"export\s*\{([^}]*)\}")
_IMPORT_NAMED = re.compile(r"""import\s*\{([^}]*)\}\s*from\s*['"]\./([^'"]+)['"]""")


def _exported_names(source: str) -> set[str]:
    names = set(_EXPORT_DECL.findall(source))
    for group in _EXPORT_LIST.findall(source):
        for item in group.split(","):
            item = item.strip()
            if item:
                names.add(item.split(" as ")[-1].strip())
    return names


def test_named_imports_are_actually_exported():
    """``import { x } from './y.js'`` の ``x`` が ``y.js`` で export されていること。

    ES modules はリンク時にここを検査するので、間違っていると **モジュールが
    1 つも評価されず画面が真っ白になる**。バンドラも型検査も無い構成なので、
    ここで受け止める。
    """
    sources = dict(_js_sources())
    exports = {name: _exported_names(src) for name, src in sources.items()}

    for name, source in sources.items():
        for group, target in _IMPORT_NAMED.findall(source):
            assert target in exports, f"{name}: import 先が無い -> {target}"
            for item in group.split(","):
                item = item.strip()
                if not item:
                    continue
                imported = item.split(" as ")[0].strip()
                assert imported in exports[target], (
                    f"{name} が import している '{imported}' は {target} で"
                    f" export されていません (export 済み: {sorted(exports[target])})"
                )


def test_referenced_dom_ids_exist_in_html():
    """JS が ``$('#...')`` で参照している id が HTML に実在すること。

    id の綴り違いは ``null`` になって初めて分かる種類の壊れ方をする
    (押しても何も起きない = まさに今回潰した体感バグと同じ見え方)。
    """
    declared = set(re.findall(r'\bid="([^"]+)"', _html_source()))

    for name, source in _js_sources():
        for used in re.findall(r"""\$\(\s*['"]#([A-Za-z0-9_-]+)['"]""", source):
            assert used in declared, f"{name} が参照する id が HTML にありません: #{used}"


@pytest.mark.parametrize(
    "module",
    ["api.js", "app.js", "data.js", "dom.js", "format.js", "health.js", "poll.js",
     "state.js", "tab-learn.js", "tab-remote.js", "tab-schedules.js",
     "tab-settings.js", "toast.js"],
)
def test_module_exists(module):
    """想定しているモジュール構成が崩れていないこと。"""
    assert (JS_DIR / module).is_file()
