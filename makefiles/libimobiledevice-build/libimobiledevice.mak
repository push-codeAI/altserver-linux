ROOT_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
include $(ROOT_DIR)/../main.mak

%.c.o : %.c
	$(CC) $(CFLAGS) $(EXTRA_FLAGS) -o $@ -c $<

%.cpp.o : %.cpp
	$(CXX) $(CXXFLAGS) $(EXTRA_FLAGS) -o $@ -c $<

CFLAGS += -DHAVE_CONFIG_H -DDEBUG -O0 -g


$(BUILD_DIR)/objs/%.c.o : $(MAIN_DIR)/%.c
	mkdir -p $(@D)
	$(CC) $(CFLAGS) $(EXTRA_FLAGS) -o $@ -c $<

$(BUILD_DIR)/objs/%.cpp.o : $(MAIN_DIR)/%.cpp
	mkdir -p $(@D)
	$(CXX) $(CXXFLAGS) $(EXTRA_FLAGS) -o $@ -c $<

include $(ROOT_DIR)/libimobiledevice-files.mak

libimobiledevice_obj := $(libimobiledevice_src:$(MAIN_DIR)/%=$(BUILD_DIR)/objs/%.o)
$(libimobiledevice_obj) : EXTRA_FLAGS := -I$(ROOT_DIR) $(libimobiledevice_include) $(libplist_include) -I$(LIB_DIR)/libimobiledevice/common -I$(LIB_DIR)/libusbmuxd/common
$(BUILD_DIR)/libimobiledevice.a : $(libimobiledevice_obj)
	ar rcs $@ $^

# --- AltServer-Linux: rewrite idevice.c at build time --------------------------------------
# See rewrite_idevice_source.py for the full reasoning. In short: a usbmux network address is a
# raw sockaddr in the layout of whichever host produced it, and the vendored parser understands
# only the BSD one. Against netmuxd -- how this project reaches the phone over Wi-Fi -- every
# refresh therefore died at "There was an error connecting to the device."
#
# libraries/libimobiledevice is a SUBMODULE, so editing it in place could not be committed here:
# only the submodule pointer would move, and it would point at a commit that does not exist
# upstream, breaking every fresh clone. Rewriting the source at build time is the same
# convention the AltSign and ldid patches already use.
idevice_patched_src := $(BUILD_DIR)/patched/libimobiledevice/idevice.c
idevice_obj := $(BUILD_DIR)/objs/libraries/libimobiledevice/src/idevice.c.o

# Written via a temp file so a failed rewrite leaves no output at all: the rewriter exits non-zero
# when a pattern stops matching, and a half-written idevice.c that still compiles would reintroduce
# the bug silently.
#
# The temp name carries the shell's PID ($$$$ -> $$ -> the pid) because THIS SUB-MAKE RUNS TWICE,
# CONCURRENTLY. The root Makefile declares `$(BUILD_DIR)/libimobiledevice.a $(BUILD_DIR)/libplist.a :`
# as one multi-target rule, which GNU make expands into two independent targets each carrying the
# same recursive recipe; both are .PHONY and both are reachable in parallel, and every shipping
# build is parallel (CI `-j3`, Dockerfile `-j$$(nproc)`). With a single fixed temp name the two
# copies race: one renames the temp away and the other's `mv` then fails the build outright with
#     mv: can't rename '.../idevice.c.tmp': No such file or directory
# Measured at 2 failures in 6 runs. A per-process temp makes the two runs independent -- they
# produce byte-identical content, so whichever renames last simply wins.
#
# The write and the rename MUST stay on one recipe line, joined by &&. Make runs each recipe line
# in its own shell, so split across two lines the two shells expand $$ to two different pids and
# the rename can never find the file it just wrote -- which fails 100% of the time, not
# intermittently. The && also keeps the rule fail-closed: a non-zero rewriter never gets renamed
# into place.
$(idevice_patched_src) : $(LIB_DIR)/libimobiledevice/src/idevice.c $(ROOT_DIR)/rewrite_idevice_source.py
	mkdir -p $(@D)
	python3 $(ROOT_DIR)/rewrite_idevice_source.py "$<" > $@.$$$$.tmp && mv $@.$$$$.tmp $@

# An explicit rule, which GNU make prefers over the $(BUILD_DIR)/objs/%.c.o pattern rule above,
# so this one object builds from the rewritten copy and every other file still builds as before.
# EXTRA_FLAGS is inherited from the $(libimobiledevice_obj) assignment; the extra -I is needed
# because the relocated copy no longer sits beside the "idevice.h" and "lockdown.h" it includes.
$(idevice_obj) : $(idevice_patched_src)
	mkdir -p $(@D)
	$(CC) $(CFLAGS) $(EXTRA_FLAGS) -I$(LIB_DIR)/libimobiledevice/src -o $@ -c $<
# -------------------------------------------------------------------------------------------

# --- AltServer-Linux: rewrite libusbmuxd.c at build time ----------------------------------------
# See rewrite_libusbmuxd_source.py: the device-event monitor reconnected with no delay and leaked
# one fd per reconnect (a mux that accepts and drops => ~1000 fds/s => "stack smashing detected"),
# and never came back after a netmuxd restart. Same temp-file/rename discipline as idevice.c above.
libusbmuxd_patched_src := $(BUILD_DIR)/patched/libusbmuxd/libusbmuxd.c
libusbmuxd_obj := $(BUILD_DIR)/objs/libraries/libusbmuxd/src/libusbmuxd.c.o

$(libusbmuxd_patched_src) : $(LIB_DIR)/libusbmuxd/src/libusbmuxd.c $(ROOT_DIR)/rewrite_libusbmuxd_source.py
	mkdir -p $(@D)
	python3 $(ROOT_DIR)/rewrite_libusbmuxd_source.py "$<" > $@.$$$$.tmp && mv $@.$$$$.tmp $@

$(libusbmuxd_obj) : $(libusbmuxd_patched_src)
	mkdir -p $(@D)
	$(CC) $(CFLAGS) $(EXTRA_FLAGS) -o $@ -c $<
# -------------------------------------------------------------------------------------------

libplist_obj := $(libplist_src:$(MAIN_DIR)/%=$(BUILD_DIR)/objs/%.o)
$(libplist_obj) : EXTRA_FLAGS := -I$(ROOT_DIR) $(libplist_include) -I$(LIB_DIR)/libplist/libcnary/include -I$(LIB_DIR)/libplist/src
$(BUILD_DIR)/libplist.a : $(libplist_obj)
	ar rcs $@ $^


#allsrc += $(libimobiledevice_src) 
#allsrc += $(libplist_src)
#allobj = $(addsuffix .o, $(allsrc))


clean::
	rm -rf $(BUILD_DIR)/patched
	rm -f $(libimobiledevice_obj)
	rm -f $(libplist_obj)
	rm -f $(BUILD_DIR)/libplist.a $(BUILD_DIR)/libimobiledevice.a
.PHONY : clean

all :: $(BUILD_DIR)/libplist.a
all :: $(BUILD_DIR)/libimobiledevice.a
.PHONY : all

.DEFAULT_GOAL := all
