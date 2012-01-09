#!/bin/sh
# Compare the reference (make-ref-repo.sh) and replay (make-replay-repo.sh)
# repositories to check for any differences/problems with the svn2svn replay.

PWD=$(pwd)
WCREF="$PWD/_wc_ref"
WCDUP="$PWD/_dup_wc"
found_diff=0

# Create a working-copy for the reference repo
# Note: We assume that the replay working-copy ("_dup_wc") still exists from make-replay-repo.sh
svn co -q file://$PWD/_repo_ref/trunk $WCREF

# Check if the final list of files is the same
echo ">> Checking file-list..."
cd $WCREF && FILESREF=$(find . -type f | grep -v "\.svn") && cd $PWD
cd $WCDUP && FILESDUP=$(find . -type f | grep -v "\.svn") && cd $PWD
if [ "$FILESREF" != "$FILESDUP" ]; then
    echo "$FILESREF" > _files_ref.txt
    echo "$FILESDUP" > _files_replay.txt
    echo "<<< _files_reference.txt"
    echo ">>> _files_replay.txt"
    diff _files_ref.txt _files_replay.txt
    rm _files_ref.txt _files_replay.txt
    found_diff=1
fi
echo ""

# Check if the final file-contents is the same
echo ">> Checking file-contents..."
cd $WCREF
FILES=$(find . -type f | grep -v "\.svn")
cd $PWD
for file in $FILES; do
    fname=$(echo "$file" | sed 's/^\.\///')
    FILEREF="$WCREF/$fname"
    FILEDUP="$WCDUP/$fname"
    if [ -f "$FILEDUP" ]; then
        chksum1=$(md5sum $FILEREF | cut -c1-32)
        chksum2=$(md5sum $FILEDUP | cut -c1-32)
        if [ "$chksum1" != "$chksum2" ]; then
            echo "Checksum mismatch: $fname"
            echo " $chksum1 $FILEREF"
            echo " $chksum2 $FILEDUP"
            found_diff=1
        fi
    else
        echo "No such file: $FILEDUP"
        found_diff=1
    fi
done
echo ""

# Clean-up
rm -rf $WCREF

# If we found any differences, exit with an error-code
[ "$found_diff" -eq 1 ] && exit 1
