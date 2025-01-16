import requests
def getReq(link, data=""):
  x = requests.get(link)
  print(link, x.status_code)
  if data:
    return x.content,data
  else:
    return x.content
