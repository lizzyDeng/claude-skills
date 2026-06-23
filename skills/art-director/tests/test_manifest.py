# tests/test_manifest.py
import json, pytest
from manifest import Asset, Manifest
BG={"id":"hero-bg","kind":"bg","prompt":"neon","aspect":"16:9","resolution":"4k","transparent":False,"path":"assets/gen/hero-bg.png","placeholder":"url(assets/gen/hero-bg.png)"}
CUT={"id":"mascot","kind":"cutout","prompt":"fox","aspect":"2:3","transparent":True,"format":"png","path":"assets/gen/mascot.png","placeholder":'src="assets/gen/mascot.png"'}
def test_load_roundtrip(tmp_path):
    p=tmp_path/"m.json"; p.write_text(json.dumps({"version":1,"style":{"brief":"x"},"assets":[BG,CUT]}))
    m=Manifest.load(str(p)); assert [a.id for a in m.assets]==["hero-bg","mascot"] and m.assets[0].task_id is None
def test_save_atomic(tmp_path):
    m=Manifest(version=1,style={},assets=[Asset(**BG)]); m.assets[0].status="done"; m.assets[0].task_id="T1"
    d=tmp_path/"sub"/".art-director"/"m.json"; m.save_atomic(str(d))
    b=Manifest.load(str(d)); assert b.assets[0].status=="done" and b.assets[0].task_id=="T1"
def test_validate_ok(): assert Manifest(version=1,style={},assets=[Asset(**BG),Asset(**CUT)]).validate()==[]
def test_dup(): assert any("duplicate" in e for e in Manifest(version=1,style={},assets=[Asset(**BG),Asset(**{**CUT,"id":"hero-bg"})]).validate())
def test_bad_kind(): assert any("kind" in e for e in Manifest(version=1,style={},assets=[Asset(**{**BG,"kind":"video"})]).validate())
def test_cutout_not_transparent(): assert any("transparent" in e for e in Manifest(version=1,style={},assets=[Asset(**{**CUT,"transparent":False})]).validate())
def test_bg_transparent(): assert any("bg" in e and "transparent" in e for e in Manifest(version=1,style={},assets=[Asset(**{**BG,"transparent":True})]).validate())
def test_bad_path(): assert any("path" in e or ".." in e for e in Manifest(version=1,style={},assets=[Asset(**{**BG,"path":"assets/gen/../x.png"})]).validate())
def test_registry_precheck_bad_aspect():
    # cutout 只允许 1:1/2:3/3:2 — validate 必须在 gen 之前抓住(codex P2)
    assert any("aspect" in e for e in Manifest(version=1,style={},assets=[Asset(**{**CUT,"aspect":"16:9"})]).validate())
def test_max():
    big=[Asset(**{**BG,"id":f"b{i}","path":f"assets/gen/b{i}.png"}) for i in range(20)]
    assert any("max_assets" in e for e in Manifest(version=1,style={},assets=big).validate(max_assets=12))
def test_style_carrier_first_bg():
    m=Manifest(version=1,style={},assets=[Asset(**CUT),Asset(**BG),Asset(**{**BG,"id":"bg2","path":"assets/gen/bg2.png"})])
    assert m.style_carrier().id=="hero-bg"   # 第一个 kind=='bg'
def test_style_carrier_no_bg_raises():
    m=Manifest(version=1,style={},assets=[Asset(**CUT)])
    with pytest.raises(ValueError) as e: m.style_carrier()
    assert "bg" in str(e.value).lower()
def test_carrier_by_id_rejects_cutout():
    m=Manifest(version=1,style={},assets=[Asset(**BG),Asset(**CUT)])
    with pytest.raises(ValueError): m.carrier_by_id("mascot")
    assert m.carrier_by_id("hero-bg").id=="hero-bg"
def test_apply_style_suffix_idempotent():
    m=Manifest(version=1,style={},assets=[Asset(**BG),Asset(**CUT)])
    changed=m.apply_style_suffix("loose oil brushwork")
    assert set(changed)=={"hero-bg","mascot"}
    assert m.assets[0].prompt.endswith("loose oil brushwork") and m.assets[1].prompt.endswith("loose oil brushwork")
    # 二次调用幂等:不再追加,无改动
    again=m.apply_style_suffix("loose oil brushwork")
    assert again==[]
    assert m.assets[0].prompt.count("loose oil brushwork")==1
def test_apply_style_suffix_skip_carrier():
    m=Manifest(version=1,style={},assets=[Asset(**BG),Asset(**CUT)])
    changed=m.apply_style_suffix("neon noir", skip_ids={"hero-bg"})
    assert changed==["mascot"] and "neon noir" not in m.assets[0].prompt
