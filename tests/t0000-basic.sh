#!/bin/bash

test_description='Test unit-test primitives
'
. ./test-lib.sh

################################################################

test_expect_success 'test success' '
    :
'
test_expect_failure 'test failure' '
    false
'
test_done
