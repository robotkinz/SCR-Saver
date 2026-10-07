CC ?= cc
CFLAGS ?= -shared -fPIC -O2
LDFLAGS ?= -ldl

.PHONY: all package clean

all: native/libscrsaver_pace.so

native/libscrsaver_pace.so: native/scrsaver_pace.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

package: all
	./package.sh

clean:
	rm -f native/libscrsaver_pace.so
	rm -rf dist
