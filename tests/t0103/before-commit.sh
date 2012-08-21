#!/bin/sh

WC=$1
REV=$2

svn mkdir -q --parents "$WC/t0103"
echo "$REV" >> "$WC/t0103/t0103.txt"
svn add -q "$WC/t0103/t0103.txt"

exit 0
