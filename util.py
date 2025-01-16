import requests
def getReq(link, data=""):
  x = requests.get(link)
  print(link, x.status_code)
  return x.content,data