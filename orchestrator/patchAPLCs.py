#!/usr/bin/env python
import sys
import copy
import os
import re


def panic(x):
    if not x.endswith("\n"):
        x += "\n"
    sys.stderr.write(x)
    sys.exit(1)


def main():
    for i in ['OBJCOPY']:
        if os.getenv(i) == None:
            panic('You must set the environment variable ' + i + ' - read the instructions. Aborting...')
    if len(sys.argv) < 5:
        panic("Usage: " + sys.argv[0] + " dir1 prefix1 dir2 prefix2 <dir3> <prefix3> <...>")
    i = 1
    dirs = []
    while i<len(sys.argv):
        dirName = sys.argv[i]
        if not dirName.endswith(os.sep):
            dirName += os.sep
        dirs.append([dirName[:], sys.argv[i+1]])
        i += 2
    for d in dirs:
        if not os.path.isdir(d[0]):
            panic("'%s' is not a directory..." % d[0])
    symbols = {}
    for (d, prefix) in dirs:
        print "Scanning symbols of object files inside:", d
        symbols[d] = set([])
        for obj in os.listdir(d):
            if not obj.endswith(".o"):
                continue
            if obj.endswith("C_ASN1_Types.o"):
                continue
            for line in os.popen("nm \"%s\"" % os.path.dirname(d) + os.sep + obj).readlines():
                if line[0] == ' ':
                    continue
                line = re.sub(r'^\S+\s+\S+\s+(.*$)', r'\1', line.strip())
                symbols[d].add(line)
    for (dirName, prefix) in dirs:
        print "Creating objcopy commands for object files in:", dirName
        uniqueSyms = copy.deepcopy(symbols[dirName])
        for (otherDir, otherPrefix) in dirs:
            if otherDir == dirName:
                continue
            uniqueSyms -= symbols[otherDir]
        patchSyms = symbols[dirName] - uniqueSyms
        if len(patchSyms) == 0:
            print "No patching necessary..."
            continue
        objcopyCmds = open(os.path.dirname(dirName) + os.sep + ".." + os.sep + "objcopyCmds", "w")
        for sym in patchSyms:
            objcopyCmds.write('%s assert_%s_%s\n' % (sym, prefix, sym))
        objcopyCmds.close()
        print "Executing objcopy commands for object files in:", dirName
        oldpwd = os.getcwd()
        os.chdir(os.path.dirname(dirName))
        os.system("for i in *.o ; do \"$OBJCOPY\" --redefine-syms ../objcopyCmds $i $i.new.o && mv $i.new.o $i ; done")
        os.chdir(oldpwd)

if __name__ == "__main__":
    main()
    sys.exit(0)
