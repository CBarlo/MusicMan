#!/usr/bin/env python3
"""
Run this on the Pi to patch config.yaml with all 9 circle slots.
Usage: python3 circles_patch.py
"""
import yaml
import sys

CONFIG = '/home/pi/musicman/config.yaml'

defaults = [
    {'id':'circle_1','name':'Circle 1','number':1,'color':'#2B5FA6','assets':{'animation_style':'spin_slam','logo':'','walkup_music':'','animation':''},'walkup':{'start_time':0,'duration':30,'fade_duration':3},'deck':{'page':1,'slot':1,'color':'#2B5FA6','label':'CIRCLE 1'},'active':True},
    {'id':'circle_2','name':'Circle 2','number':2,'color':'#CC2222','assets':{'animation_style':'color_burst','logo':'','walkup_music':'','animation':''},'walkup':{'start_time':0,'duration':30,'fade_duration':3},'deck':{'page':1,'slot':2,'color':'#CC2222','label':'CIRCLE 2'},'active':True},
    {'id':'circle_3','name':'Circle 3','number':3,'color':'#3CB96A','assets':{'animation_style':'spin_slam','logo':'','walkup_music':'','animation':''},'walkup':{'start_time':0,'duration':30,'fade_duration':3},'deck':{'page':1,'slot':3,'color':'#3CB96A','label':'CIRCLE 3'},'active':True},
    {'id':'circle_4','name':'Circle 4','number':4,'color':'#F5A623','assets':{'animation_style':'spin_slam','logo':'','walkup_music':'','animation':''},'walkup':{'start_time':0,'duration':30,'fade_duration':3},'deck':{'page':1,'slot':4,'color':'#F5A623','label':'CIRCLE 4'},'active':True},
    {'id':'circle_5','name':'Circle 5','number':5,'color':'#8B44CC','assets':{'animation_style':'color_burst','logo':'','walkup_music':'','animation':''},'walkup':{'start_time':0,'duration':30,'fade_duration':3},'deck':{'page':1,'slot':5,'color':'#8B44CC','label':'CIRCLE 5'},'active':True},
    {'id':'circle_6','name':'Circle 6','number':6,'color':'#C4610A','assets':{'animation_style':'spin_slam','logo':'','walkup_music':'','animation':''},'walkup':{'start_time':0,'duration':30,'fade_duration':3},'deck':{'page':1,'slot':6,'color':'#C4610A','label':'CIRCLE 6'},'active':True},
    {'id':'circle_7','name':'Circle 7','number':7,'color':'#8899AA','assets':{'animation_style':'spin_slam','logo':'','walkup_music':'','animation':''},'walkup':{'start_time':0,'duration':30,'fade_duration':3},'deck':{'page':1,'slot':7,'color':'#8899AA','label':'CIRCLE 7'},'active':True},
    {'id':'circle_8','name':'Circle 8','number':8,'color':'#445566','assets':{'animation_style':'spin_slam','logo':'','walkup_music':'','animation':''},'walkup':{'start_time':0,'duration':30,'fade_duration':3},'deck':{'page':1,'slot':8,'color':'#445566','label':'CIRCLE 8'},'active':True},
    {'id':'circle_9','name':'Circle 9','number':9,'color':'#CCC8C0','assets':{'animation_style':'spin_slam','logo':'','walkup_music':'','animation':''},'walkup':{'start_time':0,'duration':30,'fade_duration':3},'deck':{'page':1,'slot':9,'color':'#CCC8C0','label':'CIRCLE 9'},'active':True},
]

with open(CONFIG, 'r') as f:
    cfg = yaml.safe_load(f)

existing = {c['id']: c for c in cfg.get('circles', [])}

# Merge — keep saved data, fill in missing slots
merged = []
for d in defaults:
    if d['id'] in existing:
        # Keep saved version but ensure all keys exist
        saved = existing[d['id']]
        for k, v in d.items():
            if k not in saved:
                saved[k] = v
        merged.append(saved)
    else:
        merged.append(d)

cfg['circles'] = merged

with open(CONFIG, 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

print(f"Done — {len(merged)} circles in config")
for c in merged:
    print(f"  {c['id']}: {c['name']} ({c['color']})")
