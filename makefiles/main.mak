cur_dir := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
MAIN_DIR := $(dir $(abspath $(cur_dir)))
MAIN_DIR := $(MAIN_DIR:/=)

BUILD_DIR := $(CURDIR)

UPSTREAM_DIR := $(MAIN_DIR)/upstream_repo

LIB_DIR := $(MAIN_DIR)/libraries

# Every rewriter rule is `python3 rewrite_*.py "$<" > $@`: the shell creates $@ before the rewriter
# runs, so a guard that fails (exit 1) leaves an EMPTY target that is newer than its source. The
# next `make` then treats it as up to date and compiles the empty file -- the guard's message is
# gone and what surfaces instead is an unrelated compile or link error. This makes make delete the
# target of any recipe that fails. It is included by every makefile, so it covers all sub-makes.
.DELETE_ON_ERROR:
