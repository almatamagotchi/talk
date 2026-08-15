#!/bin/sh
# post-deploy verification for talk.almatamagotchi.com
# usage: sh verify.sh <password>
# checks the full chain through cloudflare: page, auth, session, chat.

BASE="https://talk.almatamagotchi.com"
PW="$1"
JAR=$(mktemp)

echo "== page =="
TITLE=$(curl -s --max-time 20 "$BASE/" | grep -o "<title>[^<]*</title>" | head -1)
echo "title: $TITLE"
case "$TITLE" in
  *talk*) echo "page: OK" ;;
  *) echo "page: WRONG PAGE (still old config?)" ;;
esac

echo "== wrong password =="
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" \
  -d '{"password":"definitely-wrong"}' "$BASE/api/login")
echo "wrong-pw http: $CODE"
[ "$CODE" = "401" ] && echo "wrong-pw: OK" || echo "wrong-pw: UNEXPECTED"

echo "== right password =="
RESP=$(curl -s -c "$JAR" -X POST -H "Content-Type: application/json" \
  -d "{\"password\":\"$PW\"}" "$BASE/api/login")
echo "login: $RESP"

echo "== session =="
curl -s -b "$JAR" "$BASE/api/session"; echo

echo "== chat round trip =="
curl -s -b "$JAR" -X POST -H "Content-Type: application/json" \
  -d '{"text":"greet kevin in one short spoken sentence"}' "$BASE/api/chat"; echo

rm -f "$JAR"
