CC ?= cc
CFLAGS ?= -shared -fPIC -O2
LDFLAGS ?= -ldl

.PHONY: all package clean

all: native/libscrsaver_pace.so native/afterdark_cfg.exe

native/libscrsaver_pace.so: native/scrsaver_pace.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

native/afterdark_cfg.exe: native/afterdark_cfg.c native/build-afterdark-cfg.sh
	./native/build-afterdark-cfg.sh

package: all
	./package.sh

clean:
	rm -f native/libscrsaver_pace.so
	rm -rf dist
