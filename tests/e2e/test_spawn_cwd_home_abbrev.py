"""§2.2 新建 session modal: 所有目录显示用 ~/... 缩写形式.
chips / spawn-cwd 输入框 / browse modal breadcrumb / browse confirm 写回."""
from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.pages.home_page import HomePage


def _seed_recents(page, paths):
    page.evaluate(
        '(p) => localStorage.setItem("ccr.recentCwds", JSON.stringify(p))', paths
    )
    page.reload()


def test_chips_render_abbreviated_even_if_stored_absolute(logged_in_page):
    """老用户的 localStorage 里可能存的是绝对路径; 加载时 loadRecentCwds
    要自动缩写, chip 文本显示 ~/... 形式."""
    _seed_recents(logged_in_page, ["/home/someone/codes/x",
                                    "/Users/someone/projects/y"])
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.open_new_modal()
    chips = logged_in_page.locator("#cwd-presets .chip")
    texts = chips.evaluate_all("els => els.map(e => e.textContent)")
    assert any(t.startswith("~/codes") for t in texts), (
        f"chip with /home/someone/codes/x should render as ~/codes/x; "
        f"got {texts}"
    )
    assert any(t.startswith("~/projects") for t in texts), (
        f"chip with /Users/someone/projects/y should render as ~/projects/y; "
        f"got {texts}"
    )


def test_chip_click_writes_abbreviated_to_input(logged_in_page):
    """点 chip 应该把缩写形式写到 #spawn-cwd."""
    _seed_recents(logged_in_page, ["/home/foo/code-x"])
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.open_new_modal()
    chip = logged_in_page.locator("#cwd-presets .chip").first
    expect(chip).to_be_visible()
    chip.click()
    expect(hp.spawn_cwd).to_have_value("~/code-x")


def test_browse_confirm_writes_abbreviated(logged_in_page, tmp_path, monkeypatch):
    """点 browse 'Use this' 后 spawn-cwd 应该填缩写; 这里直接 stub
    _browse.curPath 然后触发 confirm 校验."""
    page = logged_in_page
    hp = HomePage(page)
    hp.expect_visible()
    hp.open_new_modal()
    page.evaluate("""() => {
      // 打开 browse modal, 把 curPath 直接设置成绝对家目录路径
      _browse.curPath = "/home/somebody/myproj";
      document.getElementById("modal-browse").hidden = false;
      document.getElementById("modal-confirm").click();
    }""")
    expect(page.locator("#spawn-cwd")).to_have_value("~/myproj")


def test_abbreviateHome_helper_runtime(logged_in_page):
    """直接调 abbreviateHome 验证 /home/<u>/ 和 /Users/<u>/ 都被处理,
    其它路径原样保留 (不抢 /opt 之类的)."""
    page = logged_in_page
    HomePage(page).expect_visible()
    cases = page.evaluate("""() => [
      abbreviateHome("/home/u/a"),
      abbreviateHome("/Users/u/a"),
      abbreviateHome("/opt/foo"),
      abbreviateHome("~/x"),
      abbreviateHome(""),
      abbreviateHome("  /home/u  "),
    ]""")
    assert cases == [
        "~/a", "~/a", "/opt/foo", "~/x", "", "~",
    ], cases
