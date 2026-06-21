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
