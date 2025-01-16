import requests
import json
import time
import math
import random
import xmltodict
import geopy
import geopy.distance
import re
import zipfile
from os import path
import csv

#Self
import util


_CTB_Stop = {};
const_CTB_route = json.loads(util.getReq("https://rt.data.gov.hk/v2/transport/citybus/route/ctb"))
_CTB_rt = {}
_CTB_Stop_Route = {};

for i in const_CTB_route["data"]:
  _CTB_rt[i["route"]] = {"co":"CTB", "route":i["route"], 
    "I":{"route":i["route"], "bound":"I", "orig_tc":i["dest_tc"], "orig_en":i["dest_en"], "dest_tc":i["orig_tc"], "dest_en":i["orig_en"], "stops":[]},
    "O":{"route":i["route"], "bound":"O", "dest_tc":i["dest_tc"], "dest_en":i["dest_en"], "orig_tc":i["orig_tc"], "orig_en":i["orig_en"], "stops":[]}
  }
  _CTB_rt[i["route"]]["I"]["stops"] = list(map(lambda x: x["stop"],json.loads(util.getReq("https://rt.data.gov.hk/v2/transport/citybus/route-stop/ctb/"+i["route"]+"/inbound"))["data"]))
  _CTB_rt[i["route"]]["O"]["stops"] = list(map(lambda x: x["stop"],json.loads(util.getReq("https://rt.data.gov.hk/v2/transport/citybus/route-stop/ctb/"+i["route"]+"/outbound"))["data"]))


for i in _CTB_rt:
  try:
    for j in _CTB_rt[i]["I"]["stops"]:
      if not j in _CTB_Stop_Route: _CTB_Stop_Route[j] = [];
      _CTB_Stop_Route[j].append(i)
  except: pass;
  try:
    for j in _CTB_rt[i]["O"]["stops"]:
      if not j in _CTB_Stop_Route: _CTB_Stop_Route[j] = [];
      _CTB_Stop_Route[j].append(i)
  except: pass;

for i in _CTB_Stop_Route.keys():
  _CTB_Stop[i] = json.loads(util.getReq("https://rt.data.gov.hk/v2/transport/citybus/stop/"+str(i)))["data"]


with open('_CTB_Stop.json', 'w') as f:
  f.write(json.dumps(_CTB_Stop, ensure_ascii=False))
with open('_CTB_rt.json', 'w') as f:
  f.write(json.dumps(_CTB_rt, ensure_ascii=False))
with open('_CTB_Stop_Route.json', 'w') as f:
  f.write(json.dumps(_CTB_Stop_Route, ensure_ascii=False))
