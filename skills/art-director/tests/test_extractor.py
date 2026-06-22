import pytest
from manifest import Asset, Manifest
from extractor import reconcile, UnsupportedMarkup
def _man(items):  # items: list of (path, placeholder)
    return Manifest(version=1,style={},assets=[Asset(id=f"a{i}",kind="bg",prompt="p",aspect="16:9",path=p,placeholder=ph) for i,(p,ph) in enumerate(items)])
def test_ok_containment():
    m=_man([("assets/gen/h.png","url(assets/gen/h.png)"),("assets/gen/m.png",'src="assets/gen/m.png"')])
    code='<style>.h{background-image:url(assets/gen/h.png)}</style><img src="assets/gen/m.png">'
    assert reconcile(code,m).ok
def test_case_quote_space_immune():
    m=_man([("assets/gen/m.png",'SRC="assets/gen/m.png"')])
    assert reconcile('<img SRC="assets/gen/m.png">', m).ok
def test_not_wired():
    m=_man([("assets/gen/h.png","url(assets/gen/h.png)")])
    r=reconcile("<div>no assets here</div>", m); assert not r.ok and "assets/gen/h.png" in r.not_wired
def test_missing_in_manifest():
    m=_man([("assets/gen/h.png","url(assets/gen/h.png)")])
    code='<style>.h{background:url(assets/gen/h.png)}</style><img src="./assets/gen/ghost.png">'
    r=reconcile(code,m); assert not r.ok and "assets/gen/ghost.png" in r.missing_in_manifest
def test_external_ignored():
    m=_man([("assets/gen/h.png","url(assets/gen/h.png)")])
    assert reconcile('<style>.h{background:url(assets/gen/h.png)}</style><img src="https://cdn/x.png">', m).ok
def test_unsupported_only_when_asset_bearing():
    m=_man([("assets/gen/h.png","url(assets/gen/h.png)")])
    code='<style>.h{background:url(assets/gen/h.png)}</style><img srcset="https://cdn/logo.png 1x">'
    assert reconcile(code,m).ok
@pytest.mark.parametrize("bad",[
    '<img srcset="assets/gen/a.png 1x">',
    '<source srcset="assets/gen/a.png">',
    '<div style="background:image-set(url(assets/gen/a.png) 1x)">',
    '<img src={heroBg} data-asset="assets/gen/a.png">',
    '<div class="bg-[url(assets/gen/a.png)]">'])
def test_asset_bearing_unsupported_hard_fails(bad):
    m=_man([("assets/gen/a.png","x")])
    with pytest.raises(UnsupportedMarkup): reconcile(bad,m)
