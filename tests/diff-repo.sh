#!/bin/bash
# Compare the contents of two different SVN repositories.

[ $# -eq 0 ] && {
	echo "usage: $0 [repo1] [repo2]"
	exit 1
}

PWD=$(pwd)
WC1="$PWD/_wc_tmp1"
WC2="$PWD/_wc_tmp2"
found_diff=0

# Create a working-copy for the reference repo
# Note: We assume that the replay working-copy ("_wc_target") still exists from t*.sh
rm -rf "$WC1" "$WC2"
svn co -q $1 "$WC1"
svn co -q $2 "$WC2"

# Check if the final list of files is the same
cd "$WC1" && FILES1=$(find . -type f | grep -v "\.svn" | sed 's/^\.\///') && cd "$PWD"
cd "$WC2" && FILES2=$(find . -type f | grep -v "\.svn" | sed 's/^\.\///') && cd "$PWD"
if [ "$FILES1" != "$FILES2" ]; then
    echo "Found file-list differences:"
    echo "$FILES1" > _files1.txt
    echo "$FILES2" > _files2.txt
    echo "<<< @A"
    echo ">>> @B"
    diff _files1.txt _files2.txt
    rm _files1.txt _files2.txt
    found_diff=1
fi

# Check if the final file-contents is the same
cd "$WC1"
FILES=$(find . -type f | grep -v "\.svn")
cd "$PWD"
while read file; do
    fname=$(echo "$file" | sed 's/^\.\///')
    FILE1="$WC1/$fname"
    FILE2="$WC2/$fname"
    if [ -f "$FILE2" ]; then
        chksum1=$(md5sum "$FILE1" | cut -c1-32)
        chksum2=$(md5sum "$FILE2" | cut -c1-32)
        if [ "$chksum1" != "$chksum2" ]; then
            echo "Checksum mismatch: $fname"
            echo "<<< @A $fname $chksum1"
            echo ">>> @B $fname $chksum2"
            found_diff=1
        fi
    else
        found_diff=1
    fi
done < <(echo "$FILES")

# Clean-up
rm -rf "$WC1" "$WC2"

# If we found any differences, exit with an error-code
[ "$found_diff" -eq 1 ] && exit 1
exit 0
