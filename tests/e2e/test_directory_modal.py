"""§3 Directory browser modal — open / navigate / mkdir / use / cancel."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.pages.home_page import HomePage
from tests.pages.directory_modal import DirectoryModal


@pytest.fixture
def seeded_dir(tmp_path):
    """tmp_path with two known subdirs."""
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    return tmp_path


def _open_modal_at(page, hp, path: str) -> DirectoryModal:
    hp.open_new_modal()
    hp.spawn_cwd.fill(path)
    hp.browse_btn.click()
    dm = DirectoryModal(page)
    dm.expect_open()
    dm.expect_crumb(path)
    return dm


def test_open_modal_loads_current_cwd(logged_in_page, seeded_dir):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    dm = _open_modal_at(logged_in_page, hp, str(seeded_dir))
    expect(dm.row_by_name("alpha")).to_be_visible()
    expect(dm.row_by_name("beta")).to_be_visible()


def test_close_via_x(logged_in_page, seeded_dir):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    dm = _open_modal_at(logged_in_page, hp, str(seeded_dir))
    dm.close_x.click()
    dm.expect_closed()


def test_close_via_cancel(logged_in_page, seeded_dir):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    dm = _open_modal_at(logged_in_page, hp, str(seeded_dir))
    dm.cancel.click()
    dm.expect_closed()


def test_close_via_backdrop(logged_in_page, seeded_dir):
    """Clicking the modal background (the #modal-browse element itself)
    closes — handler checks e.target.id === 'modal-browse'."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    dm = _open_modal_at(logged_in_page, hp, str(seeded_dir))
    # The backdrop is the modal container itself, around the inner .modal.
    # Click at a position outside the inner modal (top-left corner area).
    dm.modal.click(position={"x": 5, "y": 5})
    dm.expect_closed()


def test_navigate_into_subdir(logged_in_page, seeded_dir):
    (seeded_dir / "alpha" / "leaf").mkdir()
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    dm = _open_modal_at(logged_in_page, hp, str(seeded_dir))

    dm.row_by_name("alpha").click()
    dm.expect_crumb(str(seeded_dir / "alpha"))
    expect(dm.row_by_name("leaf")).to_be_visible()


def test_parent_row_navigates_up(logged_in_page, seeded_dir):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    start = str(seeded_dir / "alpha")
    (seeded_dir / "alpha").mkdir(exist_ok=True)  # ensure present
    dm = _open_modal_at(logged_in_page, hp, start)

    dm.parent_row().click()
    dm.expect_crumb(str(seeded_dir))
    expect(dm.row_by_name("alpha")).to_be_visible()


def test_use_this_writes_path_back_to_spawn_cwd(logged_in_page, seeded_dir):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    target = str(seeded_dir / "alpha")
    (seeded_dir / "alpha").mkdir(exist_ok=True)
    dm = _open_modal_at(logged_in_page, hp, str(seeded_dir))

    dm.row_by_name("alpha").click()
    dm.confirm.click()
    dm.expect_closed()
    expect(hp.spawn_cwd).to_have_value(target)


def test_mkdir_via_prompt(logged_in_page, seeded_dir):
    """+ button prompts for a name, then POSTs /api/mkdir and refreshes."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    dm = _open_modal_at(logged_in_page, hp, str(seeded_dir))

    new_name = "gamma_via_test"
    logged_in_page.once(
        "dialog",
        lambda d: d.accept(prompt_text=new_name)
    )
    dm.newdir_btn.click()

    # browseLoad refreshes to the newly-created dir, so crumb becomes the new path
    dm.expect_crumb(str(seeded_dir / new_name))
    assert (seeded_dir / new_name).is_dir()
