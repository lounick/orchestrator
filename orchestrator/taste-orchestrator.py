#!/usr/bin/env python2
'''
This is the orchestrator that builds all TASTE systems - completely automating
the build process, invoking all necessary tools and code generators
'''
#
# (C) Semantix Information Technologies.
#
# Copyright 2014-2015 IB Krates <info@krates.ee>
#       QGenc code generator integration
#
# Semantix Information Technologies is licensing the code of the
# Data Modelling Tools (DMT) in the following dual-license mode:
#
# GNU GPL v. 2.1:
#       This version of DMT is the one to use for the development of
# non-commercial applications, when you are willing to comply
# with the terms of the GNU General Public License version 2.1.
#
# The features of the two licenses are summarized below:
#
#                       Commercial
#                       Developer               GPL
#                       License
#
# License cost          License fee charged     No license fee
#
# Must provide source
# code changes to DMT   No, modifications can   Yes, all source code
#                       be closed               must be provided back
#
# Can create            Yes, That is,           No, applications are subject
# proprietary           no source code needs    to the GPL and all source code
# applications          to be disclosed         must be made available
#
# Support               Yes, 12 months of       No, but available separately
#                       premium technical       for purchase
#                       support
#
# Charge for Runtimes   None                    None
#
import sys
import os
import shutil
import getopt
import re
import hashlib
import traceback
import subprocess
import copy
import time
import glob
import logging
import multiprocessing

# File handle where build log (log.txt) is
g_log = None

# Flag to control whether we stop at each command or not (useful for debug)
g_bFast = False

# Flag controlling whether we abort on error, or wait for ENTER and retry
g_bRetry = True

# Flag controlling whether we use PO-HI-Ada or PO-HI-C
g_bPolyORB_HI_C = False

# Dictionary that contains the list of Functions per partition, e.g.
# { 'mypartition_obj142' : [ 'passive_function', 'cyclic_function' ] }
g_distributionNodes = {}

# Dictionary that contains elements for both partitions and functions,
# with the values being a pair of PLATFORM, GCC prefix, e.g.
#
# { 'mypartition_obj142' : ['PLATFORM_LEON_RTEMS', 'sparc-rtems4.8-'],
#   'passive_function' : ['PLATFORM_LEON_RTEMS', 'sparc-rtems4.8-'] }

g_distributionNodesPlatform = {}

# Dictionary that points to the containing partition of a function
# (the _objNNN suffix of TASTEIV is removed from the partition name)
#
# { 'passive_function' : 'mypartition' }
g_fromFunctionToPartition = {}

# Two dictionaries that are carrying lists of special flags
# (compilation/linking) per partition, e.g.
#
# { 'mypartition' : ['-I /path/to/include', '-D_DEBUG'] }
#
# The "--nodeOptions" command line arg modifies these (and we need
# human-readable strings, not _objNNN stuff - that's why the keys
# are the humanly-named targets, not the artificially suffixed ones)
g_customCFlagsPerNode = {}
g_customLDFlagsPerNode = {}

# Discussion in Mantis ticket 278: Julien suggests using the
# compilation flags detected via the RTEMS Makefile only for
# user code compilation - ocarina already has them and dies
# otherwise.
g_customCFlagsForUserCodeOnlyPerNode = {}

# Output folder we build in
g_absOutputDir = ""

# current build stage
g_currentStage = ""

# Python logging handler, to report build stages for Peter
g_stageLog = None


class ColorFormatter(logging.Formatter):
    # FORMAT = ("[%(levelname)-19s]  " "$BOLD%(filename)-20s$RESET" "%(message)s")
    # FORMAT    = ( "[%(levelname)-18s]  " "($BOLD%(filename)s$RESET:%(lineno)d)  " "%(message)s"  )
    FORMAT = ("[%(levelname)s]  " "$BOLD$RESET" "%(message)s")
    BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)
    RESET_SEQ = "\x1b[0m"
    COLOR_SEQ = "\x1b[1;%dm"
    BOLD_SEQ = "\x1b[1m"
    COLORS = {
        'WARNING': YELLOW,
        'INFO': GREEN,
        'DEBUG': WHITE,
        'CRITICAL': RED,
        'ERROR': RED
    }

    def __init__(self, use_color):
        self.use_color = use_color
        msg = self.formatter_msg(self.FORMAT)
        logging.Formatter.__init__(self, msg)

    @staticmethod
    def bold_string(s):
        fore_color = 30 + ColorFormatter.COLORS['WARNING']
        return ColorFormatter.COLOR_SEQ % fore_color + s + ColorFormatter.RESET_SEQ

    def formatter_msg(self, msg):
        if self.use_color:
            msg = msg.replace("$RESET", self.RESET_SEQ).replace("$BOLD", self.BOLD_SEQ)
        else:
            msg = msg.replace("$RESET", "").replace("$BOLD", "")
        return msg

    def format(self, record):
        levelname = record.levelname
        if self.use_color and levelname in self.COLORS:
            fore_color = 30 + self.COLORS[levelname]
            levelname_color = self.COLOR_SEQ % fore_color + levelname + self.RESET_SEQ
            record.levelname = levelname_color
        global g_currentStage
        g_currentStage = record.getMessage()
        return logging.Formatter.format(self, record)


def panic(x):
    '''Function called to abort with a message'''
    if not x.endswith("\n"):
        x += "\n"
    sys.stderr.write(x)
    g_stageLog.error(g_currentStage)
    if g_absOutputDir != "":
        os.system('env > "' + g_absOutputDir + os.sep + 'env.txt"')
    sys.exit(1)


def mysystem(x, outputDir=None):
    '''Spawns a cmd, logs it, and if it failed, will optionally retry it when ENTER is pressed'''
    global g_log
    if g_log is None:
        g_log = open(outputDir + os.sep + "log.txt", "w")
        return
    g_log.write("From: " + os.getcwd() + "\n")
    g_log.write(x + "\n")
    g_log.flush()
    while os.system(x) != 0:
        # Save the environment that was used for the failed command under OUTPUT_FOLDER/env.txt
        if g_absOutputDir != "":
            os.system('env > "' + g_absOutputDir + os.sep + 'env.txt"')
        if g_bRetry:
            if os.getenv('CLEANUP') is not None:
                print "Exception in user code:"
                print '-' * 60
                traceback.print_stack()
                print '-' * 60
            sys.stderr.write("Failed while executing:\n" + x + "\nFrom this directory:\n" + os.getcwd())
            sys.stdout.flush()
            sys.stderr.flush()
            if os.getenv('CLEANUP') is None:
                raw_input("\n\nPress ENTER to retry...")
            else:
                panic("\nFailed to compile...")
        else:
            panic("Failed while executing:\n" + x + "\nFrom this directory:\n" + os.getcwd())


def getSingleLineFromCmdOutput(cmd):
    try:
        f = os.popen(cmd)
        returnedLine = f.readlines()[0].strip()
        if f.close() is not None:
            print "Failed! Output was:\n", returnedLine
            raise Exception()
        return returnedLine
    except:
        panic("Failed to spawn '%s'" % cmd)


def banner(msg):
    '''Splashes message in big green letters'''
    if sys.stdout.isatty():
        print "\n" + chr(27) + "[32m" + msg + chr(27) + "[0m\n"
    else:
        print "\n" + msg + "\n"
    if not g_bFast:
        raw_input("Press ENTER to continue...")
    print "\n"
    sys.stdout.flush()


def usage():
    '''Shows all available cmd line arguments'''
    panic("TASTE/ASSERT orchestrator, revision: COMMITID\n"
          "Usage: " + os.path.basename(sys.argv[0]) + " <options>\nWhere <options> are:\n\n"
          "-f, --fast\n\tSkip waiting for ENTER between stages\n\n"
          "-g, --debug\n\tEnable debuging options\n\n"
          "-p, --with-polyorb-hi-c\n\tUse PolyORB-HI-C (instead of the default, PolyORB-HI-Ada)\n\n"
          "-r, --with-coverage\n\tUse GCC coverage options (gcov) for the generated applications\n\n"
          "-h, --gprof\n\tCreate binaries that can be profiled with gprof\n\n"
          "-o, --output outputDir\n\tDirectory with generated sources and code\n\n"
          "-s, --stack stackSizeInKB\n\tHow much stack size to use (in KB)\n\n"
          "-i, --interfaceView i_view.aadl\n\tThe interface view in AADL\n\n"
          "-c, --deploymentView d_view.aadl\n\tThe deployment view in AADL\n\n"
          "-S, --subSCADE name:zipFile\n\ta zip file with the SCADE generated C code for a subsystem\n\twith the AADL name of the subsystem before the ':'\n\n"
          "-M, --subSIMULINK name:zipFile\n\ta zip file with the SIMULINK/ERT generated C code for a subsystem\n\twith the AADL name of the subsystem before the ':'\n\n"
          "-C, --subC name:zipFile\n\ta zip file with the C code for a subsystem\n\twith the AADL name of the subsystem before the ':'\n\n"
          "-B, --subCPP name:zipFile\n\ta zip file with the C++ code for a subsystem\n\twith the AADL name of the subsystem before the ':'\n\n"
          "-A, --subAda name:zipFile\n\ta zip file with the Ada code for a subsystem\n\twith the AADL name of the subsystem before the ':'\n\n"
          "-G, --subOG name:file1.pr<,file2.pr,...>\n\tObjectGeode PR files for a subsystem\n\twith the AADL name of the subsystem before the ':'\n\n"
          "-P, --subRTDS name:zipFile\n\ta zip file with the RTDS-generated code for a subsystem\n\twith the AADL name of the subsystem before the ':'\n\n"
          "-V, --subVHDL name\n\twith the AADL name of the VHDL subsystem\n\n"
          "-n, --nodeOptions name@debug=<on/off>@gcov=<on/off>@gprof=<on/off>@stackCheck=<on/off>\n\tcustom options per NODE (i.e. binary)\n\n"
          "-e, --with-extra-C-code deploymentPartition:directoryWithCfiles\n\tDirectory containing additional .c files to be compiled and linked in for deploymentPartition\n\n"
          "-d, --with-extra-Ada-code deploymentPartition:directoryWithADBfiles\n\tDirectory containing additional .adb files to be compiled and linked in for deploymentPartition\n\n"
          "-l, --with-extra-lib deploymentPartition:/path/to/libLibrary1.a<,/path/to/libLibrary2.a,...>\n\tAdditional libraries to be linked in for deploymentPartition\n\n"
          "-w, --with-cv-attributes properties_filename\n\tUpdate thread priorities, stack size and offset/phase during the build\n\n"
          "-x, --timer granularityInMilliseconds\n\tSet timer resolution (default: 100ms)")


def md5hash(filename):
    '''Returns MD5 hash of input filename'''
    a = hashlib.md5()
    a.update(open(filename, 'r').read())
    return a.hexdigest()


def mkdirIfMissing(name):
    '''Creates a directory only if it is missing'''
    if not os.path.isdir(name):
        os.mkdir(name)


def mflags(node):
    '''Returns special link flags depending on the target platform of the desired target node'''
    if node not in g_distributionNodesPlatform:
        panic("%s did not exist in the 'nodes' file..." % node)
    kind, _ = g_distributionNodesPlatform[node]
    result = ""
    if kind.startswith("PLATFORM_LINUX32"):
        result += " -m32 "
    if kind == "PLATFORM_LINUX64":
        result += " -m64 "
    # As explained in the discussion in the RTEMS mailing list...
    #
    #     https://lists.rtems.org/pipermail/users/2016-February/029782.html
    #
    # ...there is no workaround: if you want to do SPARC FPU things (and we
    # almost universally do, in all missions now) you can't mix and match RTEMS
    # compiled with -msoft-float with code compiled without it. The latest
    # build (under /opt/rtems-4.12) packaged in the TASTE VM is from the
    # master branch of RTEMS4.12, with enabled SMP; and as recommended by
    # Embedded Brains in the above discussion, the leon2/leon3/ngmp.cfg config
    # files have been modified to compile for native FPU.
    #
    # if kind.startswith("PLATFORM_LEON_RTEMS"):
    #     result += " -msoft-float "
    #
    if kind.startswith("PLATFORM_GNAT_RUNTIME"):
        result += " -mfloat-abi=hard "
        # Cortex M4's FPU does not support double precision! if C code uses double,
        # it must be forced to use float instead:
        result += " -fshort-double "
    return result


def handleXenomaiCommon(functionName, option):
    '''Returns special Xenomai compile/link flags if the input node is indeed a Xenomai one'''
    if functionName in g_distributionNodesPlatform:
        platform = g_distributionNodesPlatform[functionName][0]
        if platform.startswith("PLATFORM_LINUX32_XENOMAI"):
            skin = platform.split("_")[-1].lower()
            return " " + getSingleLineFromCmdOutput("xeno-config --skin=%s --%s" % (skin, option)) + " "
        else:
            return " "
    else:
        return " "


def handleXenomaiCflags(functionName):
    return handleXenomaiCommon(functionName, "cflags")


def handleXenomaiLDflags(functionName):
    return handleXenomaiCommon(functionName, "ldflags")


def handlePoHiC(functionName):
    '''Returns additional compilation flags for PO-HI-C builds'''
    if functionName not in g_distributionNodesPlatform:
        panic("%s did not exist in the 'nodes' file" % functionName)
    kind, _ = g_distributionNodesPlatform[functionName]

    extraCdirIncludes = ""
    polyorbActivityHpath = ""
    prefix = g_absOutputDir + "/GlueAndBuild/"
    for d in os.listdir(prefix):
        if os.path.isdir(prefix + d) and not d.startswith("glue"):
            polyorbActivityHpath = prefix + d
            break
    # In the past, we were looking for a folder under deploymentview_final
    # that started with the function name.
    #
    # This is not robust...

    # OBSOLETE
    # for d in os.listdir(polyorbActivityHpath):
    #     if os.path.isdir(polyorbActivityHpath + os.sep + d) and d.startswith(functionName):
    #         polyorbActivityHpath += os.sep + d
    #         break
    # else:

    # ...so we always check the 'nodes' info to see who is using this function.
    for partition in g_distributionNodes.keys():
        if functionName.lower() in [x.lower() for x in g_distributionNodes[partition]]:
            polyorbActivityHpath += os.sep + partition
            break
    else:
        polyorbActivityHpath = ""

    if kind == "PLATFORM_X86_LINUXTASTE":
        extraCdirIncludes += " -I$LINUXTASTE_PATH/output//target/usr/local/include -DPOSIX"
    if g_bPolyORB_HI_C and polyorbActivityHpath != "":
        extraCdirIncludes += " -I " + polyorbActivityHpath
    if g_bPolyORB_HI_C:
        extraCdirIncludes += " -I " + getSingleLineFromCmdOutput("ocarina-config --prefix") + \
            "/include/ocarina/runtime/polyorb-hi-c/include/"
    return extraCdirIncludes


def CalculateCFLAGS(node, withPOHIC=True):
    '''Uses the previous functions to create the complete set of flags for a target node'''
    if node not in g_distributionNodesPlatform:
        panic("%s did not exist in the 'nodes' file" % node)
    kind, _ = g_distributionNodesPlatform[node]
    result = " " + mflags(node) + " "
    if g_bPolyORB_HI_C and withPOHIC:
        result += handlePoHiC(node) + " "
    result += handleXenomaiCflags(node)
    if "COMPCERT" in kind:
        result += "-DWORD_SIZE=4"
    if "NDS" in kind:
        result += " -mstructure-size-boundary=8 -mcpu=arm9tdmi -mfpu=vfp -mfloat-abi=soft -mthumb-interwork -g "
    if "GUMSTIX" in kind:
        result += " -mstructure-size-boundary=8 -mcpu=xscale -mfpu=vfp -mfloat-abi=soft -g "
    if "GNAT_RUNTIME" in kind:
        result += " -DNDEBUG "  # Not supported by AdaCore's CertyFlie...

    for binary, listOfFunctions in g_distributionNodes.items():
        key = re.sub(r'_obj\d+$', '', binary)
        if node == binary or node in listOfFunctions:
            # use custom options, if available
            if key in g_customCFlagsPerNode:
                result += ' '.join(g_customCFlagsPerNode[key])
                break
    # Let the user specify -fdata-sections -ffunction-sections if he wants.
    # if "-pg" not in result and not any(map(lambda x: x in kind, ["LEON", "COMPCERT"])):
    #    result += " -fdata-sections -ffunction-sections "
    return result


def CalculateUserCodeOnlyCFLAGS(node):
    '''Uses the previous functions to create the complete set of flags for a target node'''
    if node not in g_distributionNodesPlatform:
        panic("%s did not exist in the 'nodes' file" % node)
    result = " "
    for binary, listOfFunctions in g_distributionNodes.items():
        key = re.sub(r'_obj\d+$', '', binary)
        if node == binary or node in listOfFunctions:
            # use custom options, if available
            if key in g_customCFlagsForUserCodeOnlyPerNode:
                result += ' '.join(g_customCFlagsForUserCodeOnlyPerNode[key]) + ' '
                break
    return result


def SetEnvForRTEMS(platformType):
    try:
        if platformType.startswith("PLATFORM_X86_RTEMS"):
            src = "RTEMS_MAKEFILE_PATH_X86"
            os.putenv("RTEMS_MAKEFILE_PATH", os.environ[src])
        elif platformType.startswith("PLATFORM_LEON_RTEMS"):
            src = "RTEMS_MAKEFILE_PATH_LEON"
            os.putenv("RTEMS_MAKEFILE_PATH", os.environ[src])
        elif platformType.startswith("PLATFORM_NDS_RTEMS"):
            src = "RTEMS_MAKEFILE_PATH_NDS"
            os.putenv("RTEMS_MAKEFILE_PATH", os.environ[src])
        elif platformType.startswith("PLATFORM_GUMSTIX_RTEMS"):
            src = "RTEMS_MAKEFILE_PATH_GUMSTIX"
            os.putenv("RTEMS_MAKEFILE_PATH", os.environ[src])
    except KeyError:
        panic("You must configure %s in your environment" % src)


def UpdateEnvForNode(node):
    '''Sets env vars GNAT{GCC,MAKE,BIND,LINK} and OBJCOPY, based on node's platform'''
    if node not in g_distributionNodesPlatform:
        panic("%s did not exist in the 'nodes' file" % node)
    kind, pref = g_distributionNodesPlatform[node]
    if kind == "PLATFORM_NATIVE_COMPCERT":
        os.putenv("GNATGCC", "ccomp")
    else:
        os.putenv("GNATGCC", pref + "gcc")
        os.putenv("GNATGXX", pref + "g++")
    os.putenv("GNATMAKE", pref + "gnatmake")
    os.putenv("GNATBIND", pref + "gnatbind")
    os.putenv("GNATLINK", pref + "gnatlink")
    os.putenv("OBJCOPY", pref + "objcopy")
    platformType = g_distributionNodesPlatform[node][0]
    SetEnvForRTEMS(platformType)


def DetermineNumberOfCPUs():
    """ Number of virtual or physical CPUs on this system"""
    # Python 2.6+
    try:
        return multiprocessing.cpu_count()
    except (ImportError, NotImplementedError):
        pass

    # POSIX
    try:
        res = int(os.sysconf('SC_NPROCESSORS_ONLN'))
        if res > 0:
            return res
    except (AttributeError, ValueError):
        pass

    # Windows
    try:
        res = int(os.environ['NUMBER_OF_PROCESSORS'])
        if res > 0:
            return res
    except (KeyError, ValueError):
        pass

    # BSD
    try:
        sysctl = subprocess.Popen(['sysctl', '-n', 'hw.ncpu'], stdout=subprocess.PIPE)
        scStdout = sysctl.communicate()[0]
        res = int(scStdout)
        if res > 0:
            return res
    except (OSError, ValueError):
        pass

    # Linux
    try:
        res = open('/proc/cpuinfo').read().count('processor\t:')
        if res > 0:
            return res
    except IOError:
        pass

    # Solaris
    try:
        pseudoDevices = os.listdir('/devices/pseudo/')
        expr = re.compile('^cpuid@[0-9]+$')
        res = 0
        for pd in pseudoDevices:
            if expr.match(pd) is not None:
                res += 1
        if res > 0:
            return res
    except OSError:
        pass

    # Other UNIXes (heuristic)
    try:
        try:
            dmesg = open('/var/run/dmesg.boot').read()
        except IOError:
            dmesgProcess = subprocess.Popen(['dmesg'], stdout=subprocess.PIPE)
            dmesg = dmesgProcess.communicate()[0]
        res = 0
        while '\ncpu' + str(res) + ':' in dmesg:
            res += 1
        if res > 0:
            return res
    except OSError:
        pass

    raise Exception('Can not determine number of CPUs on this system')


patternCO = re.compile(r'^.*?<compiler-option>(.*?)</compiler-option>(.*)$')
patternLO = re.compile(r'^.*?<linker-option>(.*?)</linker-option>(.*)$')


def CheckDirectives(baseDir):
    '''Scans for and returns TASTE directives for additional compilation/linking flags'''
    if os.path.exists("../directives") and os.path.exists("../directives/directives.xml"):
        for line in open("../directives/directives.xml"):
            for pattern, target in [
                    (patternCO, g_customCFlagsPerNode),
                    (patternLO, g_customLDFlagsPerNode)]:
                data = line[:]
                while True:
                    findCO = re.match(pattern, data)
                    if findCO:
                        opt = findCO.group(1)
                        data = findCO.group(2)
                        partition = g_fromFunctionToPartition[baseDir]
                        target.setdefault(partition, []).append(opt)
                    else:
                        break


def CommonBuildingPart(
    baseDir, toolDescription, CDirectories, cflagsSoFar,
    buildCmd=lambda baseDir, cf:
        mysystem("\"$GNATGCC\" -c %s -I ../../GlueAndBuild/glue%s/ -I ../../auto-src/ *.c" % (cf, baseDir))):

    '''The common build sequence for C, SCADE, Simulink'''

    if not os.path.isdir(baseDir):
        panic("No directory %s! (pwd=%s)" % (baseDir, os.getcwd()))
    if not os.path.isdir(baseDir + os.sep + baseDir):
        panic("%s zip file did not contain a %s dir..." % (toolDescription, baseDir))
    os.chdir(baseDir + os.sep + baseDir)
    CheckDirectives(baseDir)
    mysystem("for i in %s_vm_if.c %s_vm_if.h %s.h ; do if [ -f ../$i ] ; then cp ../$i . ; fi ; done" %
             (baseDir, baseDir, baseDir))
    mysystem("for i in hpredef.h invoke_ri.c ; do if [ -f ../$i ] ; then cp ../$i . ; fi ; done")
    mysystem("cp ../*polyorb_interface.? . 2>/dev/null || exit 0")
    mysystem("cp ../Context-*.? . 2>/dev/null || exit 0")
    mysystem("rm -f ../*-uniq.? *-uniq.? 2>/dev/null || exit 0")
    mysystem("rm -f ../dataview.[ch] dataview.* 2>/dev/null || exit 0")
    extraCdirIncludes = " "
    partitionName = g_fromFunctionToPartition[baseDir]
    if partitionName in CDirectories:
        for d in CDirectories[partitionName]:
            extraCdirIncludes += "-I \"" + d + "\" "
    if (baseDir in g_distributionNodesPlatform.keys()):
        UpdateEnvForNode(baseDir)
    cflags = cflagsSoFar + extraCdirIncludes + CalculateCFLAGS(baseDir) + CalculateUserCodeOnlyCFLAGS(baseDir)
    # Add include path to glue code
    cflags += " -I ../../GlueAndBuild/glue" + baseDir + "/ "
    buildCmd(baseDir, cflags)
    os.chdir("../..")


def BuildSCADEsystems(scadeSubsystems, CDirectories, cflagsSoFar):
    '''Compiles all user code for SCADE Functions'''
    if scadeSubsystems:
        g_stageLog.info("Building SCADE subSystems")
    for baseDir in scadeSubsystems.keys():
        CommonBuildingPart(baseDir, "SCADE", CDirectories, cflagsSoFar)


def BuildSimulinkSystems(simulinkSubsystems, CDirectories, cflagsSoFar, bUseSimulinkMakefiles):
    '''Compiles all user code for Simulink Functions'''
    if simulinkSubsystems:
        g_stageLog.info("Building Simulink subSystems")

    def buildCmdSimulink(baseDir, cf):
        if bUseSimulinkMakefiles[baseDir][0]:
            mysystem('make -f "' + bUseSimulinkMakefiles[baseDir][1] + '" assertBuild')
            if g_bPolyORB_HI_C:
                mysystem("\"$GNATGCC\" -c %s *polyorb_interface.c" % cf)
        else:
            mysystem("\"$GNATGCC\" -c %s *.c" % cf)
    for baseDir in simulinkSubsystems.keys():
        CommonBuildingPart(baseDir, "Simulink", CDirectories, cflagsSoFar, buildCmdSimulink)


def BuildCsystems(cSubsystems, CDirectories, cflagsSoFar):
    '''Compiles all user code for C Functions'''
    if cSubsystems:
        g_stageLog.info("Building C subSystems")
    for baseDir in cSubsystems.keys():
        CommonBuildingPart(baseDir, "C", CDirectories, cflagsSoFar)


def BuildCPPsystems(cppSubsystems, CDirectories, cflagsSoFar):
    '''Compiles all user code for C++ Functions'''
    if cppSubsystems:
        g_stageLog.info("Building C++ subSystems")

    def buildCmdCPP(baseDir, cf):
        mysystem("\"$GNATGXX\" -c %s -I ../../GlueAndBuild/glue%s/ -I ../../auto-src/ *.cc" % (cf, baseDir))
        mysystem("\"$GNATGCC\" -c %s -I ../../GlueAndBuild/glue%s/ -I ../../auto-src/ *.c" % (cf, baseDir))
    for baseDir in cppSubsystems.keys():
        CommonBuildingPart(baseDir, "C++", CDirectories, cflagsSoFar, buildCmdCPP)


def BuildAdaSystems_C_code(adaSubsystems, unused_CDirectories, uniqueSetOfAdaPackages, cflagsSoFar):
    '''Compiles all C bridge code for Ada Functions (Ada user code compiled via Ocarina Makefiles)'''
    if adaSubsystems:
        g_stageLog.info("Building Ada subSystems")
    for baseDir in adaSubsystems.keys():
        if not os.path.isdir(baseDir):
            panic("No directory %s! (pwd=%s)" % (baseDir, os.getcwd()))
        if not os.path.isdir(baseDir + os.sep + baseDir):
            panic("Ada zip file did not contain a %s dir..." % (baseDir))
        os.chdir(baseDir + os.sep + baseDir)
        CheckDirectives(baseDir)
        mysystem("for i in `/bin/ls ../../GlueAndBuild/glue%s/*.ad? 2>/dev/null | grep -v '/asn1_'` ; do cp \"$i\"  . ; done" % baseDir)
        # mysystem("cp ../../GlueAndBuild/glue%s/asn1_types.ads ." % baseDir)
        mysystem("cp ../../GlueAndBuild/glue%s/adaasn1rtl.ad? . 2>/dev/null ; exit 0" % baseDir)
        for modulebase in uniqueSetOfAdaPackages.keys():
            mysystem("cp ../../GlueAndBuild/glue%s/%s.ad? . 2>/dev/null ; exit 0" % (baseDir, modulebase))
        mysystem("for i in %s_vm_if.c %s_vm_if.h vm_callback.c ; do if [ -f ../$i ] ; then cp ../$i . ; fi ; done" %
                 (baseDir, baseDir))
        mysystem("for i in hpredef.h invoke_ri.c vm_callback.h ; do if [ -f ../$i ] ; then cp ../$i . ; fi ; done")
        mysystem("cp ../*polyorb_interface.h . 2>/dev/null || exit 0")
        mysystem("rm -f ../dataview.ad[sb] 2>/dev/null || exit 0")
        # obsolete: compilation of Ada code is done via Ocarina's makefiles, not by the orchestrator
        # mysystem("\"$GNATGCC\" -g -c *.adb")
        # mysystem("for i in *.ads ; do [ ! -f ${i/.ads/.adb} ] && \"$GNATGCC\" -g -c *.ads || break ; done")
        if (baseDir in g_distributionNodesPlatform.keys()):
            UpdateEnvForNode(baseDir)
        mysystem("\"$GNATGCC\" -c %s -I ../../GlueAndBuild/glue%s/ -I ../../auto-src/ *.c" %
                 (cflagsSoFar + CalculateCFLAGS(baseDir) + CalculateUserCodeOnlyCFLAGS(baseDir), baseDir))
        os.chdir("..")
        cflags = cflagsSoFar + CalculateCFLAGS(baseDir) + CalculateUserCodeOnlyCFLAGS(baseDir)
        mysystem("\"$GNATGCC\" -c -I ../GlueAndBuild/glue%s/ -I ../auto-src/ %s *.c" % (baseDir, cflags))
        os.chdir("..")


def BuildObjectGeodeSystems(ogSubsystems, CDirectories, cflagsSoFar):
    '''Compiles all user code for ObjectGeode Functions'''
    if ogSubsystems:
        g_stageLog.info("Building ObjectGeode subSystems")
    for ss in ogSubsystems.keys():
        base = os.path.basename(ss)
        baseDir = os.path.splitext(base)[0]
        if not os.path.isdir(baseDir + os.sep + "ext"):
            panic("OG subsystems must contain an ext/ directory! (%s)" % str(ss))
        # This is for ObjectGeode code
        os.chdir(baseDir + os.sep + "ext")
        CheckDirectives(baseDir)
        mysystem("if [ ! -f \"$WORKDIR/GlueAndBuild/glue%s/OG_ASN1_Types.h\" ] ; then touch \"$WORKDIR/GlueAndBuild/glue%s/OG_ASN1_Types.h\" ; fi" % (ss, ss))
        mysystem("cp ../*polyorb_interface.? . 2>/dev/null || exit 0")
        mysystem("cp ../Context-*.? . 2>/dev/null || exit 0")
        mysystem("rm -f ../*-uniq.? *-uniq.? 2>/dev/null || exit 0")
        mysystem("rm -f ../dataview.[ch] 2>/dev/null || exit 0")
        extraCdirIncludes = " "
        partitionName = g_fromFunctionToPartition[baseDir]
        if partitionName in CDirectories:
            for d in CDirectories[partitionName]:
                extraCdirIncludes += "-I \"" + d + "\" "
        if (baseDir in g_distributionNodesPlatform.keys()):
            UpdateEnvForNode(baseDir)
        mysystem("for i in *.c ; do \"$GNATGCC\" -c %s -I \"$WORKDIR/auto-src/\"  -I \"$WORKDIR/GlueAndBuild/glue%s/\" \"$i\" || exit 1 ; done" %
                 (cflagsSoFar + extraCdirIncludes + CalculateCFLAGS(ss) + CalculateUserCodeOnlyCFLAGS(ss), ss))
        os.chdir("../..")


def BuildRTDSsystems(rtdsSubsystems, CDirectories, cflagsSoFar):
    '''Compiles all user code for PragmaDev Functions'''
    if rtdsSubsystems:
        g_stageLog.info("Building RTDS subSystems")
    for baseDir in rtdsSubsystems.keys():
        if not os.path.isdir(baseDir):
            panic("No directory %s! (pwd=%s)" % (baseDir, os.getcwd()))
        if not os.path.isdir(baseDir + os.sep + baseDir):
            panic("RTDS zip file did not contain a %s dir..." % (baseDir))
        os.chdir(baseDir + os.sep + baseDir)
        CheckDirectives(baseDir)
        mysystem("for i in common.h invoke_ri.c %s_vm_if.c %s_vm_if.h glue_%s.h glue_%s.c profile/RTDS_Proc.c ; do if [ -f ../$i ] ; then cp ../$i . ; fi ; done" %
                 (baseDir, baseDir, baseDir, baseDir))
        mysystem("cp ../*polyorb_interface.? . 2>/dev/null || exit 0")
        mysystem("cp ../Context-*.? . 2>/dev/null || exit 0")
        mysystem("rm -f ../*-uniq.? *-uniq.? 2>/dev/null || exit 0")
        mysystem("cp ../*syncRI.c . 2>/dev/null || exit 0")
        mysystem("rm -f ../dataview.[ch] 2>/dev/null || exit 0")
        extraCdirIncludes = " "
        partitionName = g_fromFunctionToPartition[baseDir]
        if partitionName in CDirectories:
            for d in CDirectories[partitionName]:
                extraCdirIncludes += "-I \"" + d + "\" "
        if (baseDir in g_distributionNodesPlatform.keys()):
            UpdateEnvForNode(baseDir)
        mysystem("\"$GNATGCC\" -c -DRTDS_NO_SCHEDULER %s %s -I ../../GlueAndBuild/glue%s/ -I ../../auto-src/ -I ../profile *.c" %
                 (cflagsSoFar + CalculateCFLAGS(baseDir) + CalculateUserCodeOnlyCFLAGS(baseDir), extraCdirIncludes, baseDir))
        os.chdir("../..")


def BuildVHDLsystems_C_code(vhdlSubsystems, CDirectories, cflagsSoFar):
    '''Compiles all C bridge code for VHDL Functions'''
    if vhdlSubsystems:
        g_stageLog.info("Building C code of VHDL subSystems")
    for baseDir in vhdlSubsystems.keys():
        if not os.path.isdir(baseDir):
            panic("No VHDL directory %s! (pwd=%s)" % (baseDir, os.getcwd()))
        os.chdir(baseDir)
        extraCdirIncludes = ""
        partitionName = g_fromFunctionToPartition[baseDir]
        if partitionName in CDirectories:
            for d in CDirectories[partitionName]:
                extraCdirIncludes += "-I \"" + d + "\" "
        if len([x for x in os.listdir(".") if x.endswith("polyorb_interface.c")])>0:
            if (baseDir in g_distributionNodesPlatform.keys()):
                UpdateEnvForNode(baseDir)
            mysystem("\"$GNATGCC\" -c %s %s -I ../GlueAndBuild/glue%s/ -I ../auto-src/ *.c" %
                     (cflagsSoFar + CalculateCFLAGS(baseDir) + CalculateUserCodeOnlyCFLAGS(baseDir), extraCdirIncludes, baseDir))
        os.chdir("..")


def BuildGUIs(guiSubsystems, cflagsSoFar, asn1Grammar):
    '''Builds automatically generated wxWdigets GUIs'''
    if guiSubsystems:
        g_stageLog.info("Building automatically created GUIs")
    commentedGUIfilters = []
    for baseDir in guiSubsystems:
        if not os.path.isdir(baseDir):
            panic("No directory %s! (pwd=%s)" % (baseDir, os.getcwd()))
        # This is for GUI code
        if not os.path.exists(baseDir + os.sep + baseDir + "_gui_code.c"):
            panic("GUI generated code did not contain a %s ..." % (baseDir + os.sep + baseDir + "_gui_code.c"))
        os.chdir(baseDir)
        mkdirIfMissing("ext")
        mysystem('for i in * ; do if [ -f "$i" -a ! -e ext/"$i" ] ; then ln -s ../"$i" ext/ ; fi ; done')
        os.chdir("ext")
        partitionNameWithoutSuffix = g_fromFunctionToPartition.get(baseDir, None)
        if partitionNameWithoutSuffix:
            for k, v in g_distributionNodesPlatform.iteritems():
                if k.startswith(partitionNameWithoutSuffix):
                    platform = v[0]
                    break
            else:
                platform = None
            if platform == 'PLATFORM_WIN32':
                installPath = getSingleLineFromCmdOutput("taste-config --prefix")
                mysystem("%s/share/gui-udp/build_gui_glue.py %s" % (installPath, baseDir))
                mysystem('cp "%s"/share/gui-udp/udpcontroller.? .' % installPath)
            else:
                mysystem("cp \"$DMT\"/AutoGUI/queue_manager.? .")
        mysystem("cp ../*polyorb_interface.? . 2>/dev/null || exit 0")
        mysystem("cp ../Context-*.? . 2>/dev/null || exit 0")
        mysystem("rm -f ../*-uniq.? *-uniq.? 2>/dev/null || exit 0")
        if (baseDir in g_distributionNodesPlatform.keys()):
            UpdateEnvForNode(baseDir)
        mysystem("\"$GNATGCC\" -c %s -I ../../GlueAndBuild/glue%s/ -I ../../auto-src/ *.c" %
                 (cflagsSoFar + CalculateCFLAGS(baseDir) + CalculateUserCodeOnlyCFLAGS(baseDir), baseDir))
        os.chdir("..")
        # Now create the controlling GUI application
        mkdirIfMissing("GUI")
        mysystem('for i in * ; do if [ -f "$i" -a ! -e GUI/"$i" ] ; then ln -s ../"$i" GUI/ ; fi ; done')
        os.chdir("GUI")
        mysystem("cp \"$DMT\"/AutoGUI/* .")
        mysystem("cat Makefile | sed 's,DataView,%s,g' > a_temp_name && mv a_temp_name Makefile" % os.path.splitext(os.path.basename(asn1Grammar))[0])
        mysystem("cat Makefile | sed 's,applicationName,%s,g' > a_temp_name && mv a_temp_name Makefile" % (baseDir + "_GUI"))
        mysystem("cp -u ../../GlueAndBuild/glue" + baseDir + "/C_*.[ch] .")
        # mysystem("cp ../auto-src/* .")
        if baseDir.endswith('probe_console'):
            os.chdir("../..")
            continue
        os.chdir("../..")
    return commentedGUIfilters


def BuildPythonStubs(pythonSubsystems, asn1Grammar, acnFile):
    '''Builds automatically generated Python stubs'''
    if pythonSubsystems:
        g_stageLog.info("Building automatically created Python stubs")
    for baseDir in pythonSubsystems:
        if not os.path.isdir(baseDir):
            panic("No directory %s! (pwd=%s)" % (baseDir, os.getcwd()))
        olddir = os.getcwd()
        pattern = re.compile(r'.*?glue([^/]*)')
        findFV = re.match(pattern, baseDir)
        if findFV:
            FVname = findFV.group(1)
        else:
            panic("Could not detect FVname out of '%s'" % baseDir)
        os.chdir(baseDir)
        mysystem("cp \"$DMT\"/AutoGUI/queue_manager.? .")
        mysystem("cp \"$DMT\"/AutoGUI/timeInMS.? .")
        mysystem("cp \"$DMT\"/AutoGUI/debug_messages.? .")
        mysystem("cp \"%s\"/%s/%s_enums_def.h ." % (g_absOutputDir, FVname, FVname))
        mysystem("cp \"%s\" ." % asn1Grammar)
        mysystem("cp \"%s\" ." % acnFile)
        mkdirIfMissing("asn2dataModel")
        mysystem("asn2dataModel -o asn2dataModel -toPython " + os.path.basename(asn1Grammar))
        os.chdir("asn2dataModel")
        mysystem("cp \"%s\" ." % acnFile)

        guiName = re.sub(r'^.*/glue(.*)/.*$', '\\1', baseDir)
        partitionNameWithoutSuffix = g_fromFunctionToPartition.get(guiName, None)
        if partitionNameWithoutSuffix:
            for k, v in g_distributionNodesPlatform.iteritems():
                if k.startswith(partitionNameWithoutSuffix):
                    platform = v[0]
                    break
            else:
                platform = None
            if platform == 'PLATFORM_WIN32':
                installPath = getSingleLineFromCmdOutput("taste-config --prefix")
                mysystem('cp "%s"/share/gui-udp/Makefile.python .' % installPath)
        mysystem("cp \"%s\"/%s/interface_enum.h ." % (g_absOutputDir, FVname))
        mysystem("make -f Makefile.python")
        os.chdir("..")
        mysystem("cp asn2dataModel/asn1crt.h asn2dataModel/Stubs.py asn2dataModel/DV* asn2dataModel/*.so .")
        mysystem("cp asn2dataModel/%s.h ." % os.path.splitext(os.path.basename(asn1Grammar))[0])
        mysystem("cp asn2dataModel/%s_asn.py ." % os.path.splitext(os.path.basename(asn1Grammar))[0].replace("-", "_"))
        # mysystem("swig  -Wall -includeall -outdir . -python ./PythonAccess.i")
        # mysystem("gcc -g -fPIC -c `python-config --cflags` gui_api.c queue_manager.c timeInMS.c debug_messages.c PythonAccess_wrap.c")
        mysystem("gcc -g -fPIC -c `python-config --cflags` gui_api.c queue_manager.c timeInMS.c debug_messages.c")
        # mysystem("gcc -g -shared -o _PythonAccess.so PythonAccess_wrap.o gui_swig.o queue_manager.o timeInMS.o debug_messages.o `python-config --ldflags` -lrt")
        mysystem("gcc -g -shared -o PythonAccess.so gui_api.o queue_manager.o timeInMS.o debug_messages.o `python-config --ldflags` -lrt")
        os.chdir(olddir)


def BuildCyclicSubsystems(cyclicSubsystems, cflagsSoFar):
    '''Compiles code of Cyclic Functions'''
    if cyclicSubsystems:
        g_stageLog.info("Building cyclic subSystems")
    for baseDir in cyclicSubsystems:
        if not os.path.isdir(baseDir):
            panic("No directory %s! (pwd=%s)" % (baseDir, os.getcwd()))
        # This is for automatically generated Cyclic code
        os.chdir(baseDir + os.sep)
        if (baseDir in g_distributionNodesPlatform.keys()):
            UpdateEnvForNode(baseDir)
        if 0 != len([x for x in os.listdir(".") if x.endswith(".c")]):
            mysystem("\"$GNATGCC\" -c %s -I ../GlueAndBuild/glue%s/ -I ../auto-src/ *.c" %
                     (cflagsSoFar + CalculateCFLAGS(baseDir) + CalculateUserCodeOnlyCFLAGS(baseDir), baseDir))
        os.chdir("..")


def RenameCommonlyNamedSymbols(scadeSubsystems, simulinkSubsystems, cSubsystems, cppSubsystems, adaSubsystems, rtdsSubsystems, ogSubsystems, guiSubsystems, cyclicSubsystems, vhdlSubsystems):
    '''Identifies and renames identical symbols in separate subsystems'''
    g_stageLog.info("Renaming commonly named symbols")

    def getTarget(baseDir):
        if baseDir in ogSubsystems or baseDir in guiSubsystems:
            return "/ext/"
        elif baseDir in cyclicSubsystems or baseDir in vhdlSubsystems:
            return os.sep
        else:
            return os.sep + baseDir + os.sep

    prefixes = {}
    for baseDir in scadeSubsystems.keys() + simulinkSubsystems.keys() + cSubsystems.keys() + cppSubsystems.keys() + \
            adaSubsystems.keys() + rtdsSubsystems.keys() + ogSubsystems.keys() + \
            guiSubsystems + cyclicSubsystems + vhdlSubsystems.keys():
        if baseDir not in g_distributionNodesPlatform:
            panic("%s did not exist in the 'nodes' file" % baseDir)
        systemPlatform, pref = g_distributionNodesPlatform[baseDir]
        prefixes[pref] = systemPlatform
        appendTarget = getTarget(baseDir)
        if 0 != len([x for x in os.listdir("GlueAndBuild/glue" + baseDir) if x.endswith(".o")]):
            mysystem("mv GlueAndBuild/glue" + baseDir + "/*.o " + baseDir + appendTarget)

    for prefix, systemPlatform in prefixes.items():
        renamingDirs = 0
        cmd = "patchAPLCs.py "
        for baseDir in scadeSubsystems.keys() + simulinkSubsystems.keys() + cSubsystems.keys() + cppSubsystems.keys() + \
                adaSubsystems.keys() + rtdsSubsystems.keys() + ogSubsystems.keys() + \
                guiSubsystems + cyclicSubsystems + vhdlSubsystems.keys():
            _, pref = g_distributionNodesPlatform[baseDir]
            if pref != prefix:
                continue
            appendTarget = getTarget(baseDir)
            cmd += ' "' + baseDir + appendTarget + '"'
            cmd += ' "' + baseDir.replace(' ', '_') + '_renamed"'
            renamingDirs += 1
            if baseDir not in adaSubsystems:
                cmd += ' "' + baseDir + appendTarget + '"'
                cmd += ' "' + baseDir.replace(' ', '_') + '"'
                renamingDirs += 1
        if renamingDirs > 1:
            os.putenv("OBJCOPY", prefix + "objcopy")
            asn1SccFolder = "auto-src_" + systemPlatform
            if os.path.isdir(asn1SccFolder):
                cmd += ' ' + asn1SccFolder + "/"
                cmd += ' ' + asn1SccFolder
            mysystem(cmd)


def InvokeOcarinaMakefiles(
    scadeSubsystems, simulinkSubsystems, cSubsystems, cppSubsystems, adaSubsystems, rtdsSubsystems, ogSubsystems, guiSubsystems, cyclicSubsystems, vhdlSubsystems,
        cflagsSoFar, CDirectories, AdaDirectories, AdaIncludePath, ExtraLibraries,
        bDebug, bUseEmptyInitializers, bCoverage, bProfiling):

    '''Invokes Makefiles generated by Ocarina - generates final executable code'''
    g_stageLog.info("Invoking Ocarina generated Makefiles")
    os.chdir(g_absOutputDir)
    os.chdir("GlueAndBuild")
    for root, _, files in os.walk("."):
        for _ in [x for x in files if x.lower() == "makefile"]:
            # Learn the name of the AADL system
            node = ""
            for line in open(root + os.sep + "Makefile").readlines():
                line = line.strip()
                if line.startswith("#  Node name"):
                    node = line[34:]
            if node == "":
                # Handle LaTEX-doxygen related Makefiles (closes: #488)
                continue
            if node not in g_distributionNodes:
                panic("There is no '%s' node in the distribution nodes generated by buildsupport." % node)

            partitionNameWithoutSuffix = re.sub(r'_obj\d+$', '', node)

            # Create the EXTERNAL_OBJECTS line
            externals = ""
            userCFlags = "-g " if bDebug else ""
            userLDFlags = "-g " if bDebug else ""

            # VCD support
            if bDebug and g_bPolyORB_HI_C:
                userCFlags += "-D__PO_HI_USE_VCD=1 "

            # Profiling: Either the global "--gprof" has been used,
            # or the node-specific "-n nodeName@gprof=on" has been used.
            # or no profiling is needed.

            # Has the user asked for this specific node to be profiled?
            nodeIsGPROFed = partitionNameWithoutSuffix in g_customCFlagsPerNode and \
                "-pg" in g_customCFlagsPerNode[partitionNameWithoutSuffix]
            # Or maybe the user passed "--gprof" ?
            nodeIsGPROFed = nodeIsGPROFed or bProfiling
            # In either case, setup profiling:
            if nodeIsGPROFed:
                userCFlags += " -D__PO_HI_USE_GPROF "
                os.putenv("USE_GPROF", "1")
                if g_distributionNodesPlatform[node][0].startswith("PLATFORM_LEON_RTEMS"):
                    userCFlags += " -I " + os.getenv("RTEMS_MAKEFILE_PATH_LEON") + "/lib/include/ "
            else:
                # Otherwise disable it
                os.unsetenv("USE_GPROF")
            # If global ("--gprof") profiling is set, add "-pg" to CFLAGS and LDFLAGS
            if bProfiling:
                userCFlags += " -pg "
                userLDFlags += " -pg "

            # With AADLv2, we support multi-platform builds, so we must compile the code with the appropriate compiler
            UpdateEnvForNode(node)
            olddir = os.getcwd()

            # Check to see if we are using pohic and building a system with Ada parts.
            bNeedAdaBuildWorkaround = False
            for aplc in g_distributionNodes[node]:
                if aplc in adaSubsystems.keys():
                    if g_bPolyORB_HI_C:
                        bNeedAdaBuildWorkaround = True
                        break

            os.chdir("..")
            asn1target = "auto-src_" + g_distributionNodesPlatform[node][0]
            asn1target = os.path.abspath(asn1target)
            poHiAdaLinkCmd = ""
            # in case of rebuilds
            mysystem("rm -rf \"%s\" 2>/dev/null ; exit 0" % asn1target)
            if not os.path.exists(asn1target):
                os.mkdir(asn1target)
                os.chdir(asn1target)
                mysystem("cp ../auto-src/*.[ch] .")
                mysystem("\"$GNATGCC\" -c %s *.c" % (cflagsSoFar + CalculateCFLAGS(node) + CalculateUserCodeOnlyCFLAGS(node)))
                os.chdir("..")

            if bNeedAdaBuildWorkaround:
                os.chdir(asn1target)
                for baseDir, ss in adaSubsystems.items():
                    if baseDir not in g_distributionNodes[node]:
                        continue
                    mysystem("cp ../GlueAndBuild/glue" + baseDir + "/*.adb . 2>/dev/null || exit 0")
                    mysystem("cp ../GlueAndBuild/glue" + baseDir + "/*.ads . 2>/dev/null || exit 0")
                TasteAda = open('tasteada.ads', 'w')
                for baseDir, ss in adaSubsystems.items():
                    if baseDir not in g_distributionNodes[node]:
                        continue
                    TasteAda.write('with %s;\n' % baseDir)
                TasteAda.write('package TasteAda is\n')
                TasteAda.write('end TasteAda;\n')
                TasteAda.close()
                # open("conf.ec",'w').write("pragma No_Run_Time;\n")
                # mysystem("gnatmake -c -I../../auto-src " + baseDir + " " + x + " -gnatec=conf.ec")
                mysystem("\"$GNATMAKE\" -c %s -I.  -gnat2012 tasteada.ads" % mflags(node))
                if not os.path.exists("tasteada.ali"):
                    panic("WARNING: No tasteada.ali file was generated")
                mysystem("\"$GNATBIND\" -t -n tasteada.ali -o ada-start.adb")
                dbg = "-g" if bDebug else ""
                mysystem("\"$GNATMAKE\" -c %s %s -gnat2012 ada-start.adb" % (dbg, mflags(node)))
                for line in open("ada-start.adb").readlines():
                    if -1 != line.find("adalib"):
                        poHiAdaLinkCmd = line.strip().replace("--", "")
                        runtimePath = " " + line.strip().replace("-L", "-Wl,-R") + " "
                        poHiAdaLinkCmd += runtimePath.replace("--", "")
                if poHiAdaLinkCmd == "":
                    panic("There was no line containing 'adalib' inside 'ada-start.adb'")
                poHiAdaLinkCmd += " -lgnat -lgnarl"
                os.chdir("..")

            externals += asn1target + '/*.o '
            if g_distributionNodesPlatform[node][0] in ("PLATFORM_LINUX32",):  # and platform.architecture()[0] == '64bit':
                userCFlags += ' -m32 '
                userLDFlags += ' -m32 '
            userLDFlags += handleXenomaiLDflags(node)
            os.chdir(olddir)

            for aplc in g_distributionNodes[node]:
                for baseDir in scadeSubsystems.keys() + simulinkSubsystems.keys() + cSubsystems.keys() + cppSubsystems.keys() + adaSubsystems.keys() + rtdsSubsystems.keys():
                    if baseDir == aplc:
                        if g_bPolyORB_HI_C and baseDir in adaSubsystems:
                            if 0 != len([x for x in os.listdir(g_absOutputDir + os.sep + baseDir + os.sep + baseDir + os.sep) if x.endswith('.o')]):
                                externals += g_absOutputDir + os.sep + baseDir + os.sep + baseDir + os.sep + '*.o '
                            if 0 != len([x for x in os.listdir(g_absOutputDir + os.sep + baseDir + os.sep) if x.endswith('.o')]):
                                for u in ("%s_vm_if.o" % baseDir, "invoke_ri.o"):
                                    mysystem("rm -f %s/%s" % (g_absOutputDir + os.sep + baseDir + os.sep, u))
                                externals += g_absOutputDir + os.sep + baseDir + os.sep + '*.o '
                        else:
                            externals += g_absOutputDir + os.sep + baseDir + os.sep + baseDir + os.sep + '*.o '
                for ss in ogSubsystems.keys():
                    if ss == aplc:
                        base = os.path.basename(ss)
                        baseDir = os.path.splitext(base)[0]
                        externals += g_absOutputDir + os.sep + baseDir + os.sep + "ext" + os.sep + '*.o '
                for ss in guiSubsystems:
                    if ss == aplc:
                        base = os.path.basename(ss)
                        baseDir = os.path.splitext(base)[0]
                        externals += g_absOutputDir + os.sep + baseDir + os.sep + "ext" + os.sep + '*.o '
                for ss in cyclicSubsystems:
                    if ss == aplc:
                        base = os.path.basename(ss)
                        baseDir = os.path.splitext(base)[0]
                        if 0 != len([x for x in os.listdir(g_absOutputDir + os.sep + baseDir + os.sep) if x.endswith('.c')]):
                            externals += g_absOutputDir + os.sep + baseDir + os.sep + '*.o '
                for vhdlSubsystem in vhdlSubsystems.keys():
                    if vhdlSubsystem == aplc:
                        base = os.path.basename(vhdlSubsystem)
                        baseDir = os.path.splitext(base)[0]
                        externals += g_absOutputDir + os.sep + baseDir + os.sep + '*.o '

            # Extra C code
            if partitionNameWithoutSuffix in CDirectories:
                for extraCdir in CDirectories[partitionNameWithoutSuffix]:
                    g_stageLog.info("Compiling additional C code in '%s'..." % extraCdir)
                    if len([x for x in os.listdir(extraCdir) if x.endswith(".c")])!=0:
                        pwd = os.getcwd()
                        os.chdir(extraCdir)
                        # banner("You use AADLv2 and external code, I don't know what flags to compile it with!!!")
                        if bUseEmptyInitializers:
                            mysystem("\"$GNATGCC\" %s -c -DEMPTY_LOCAL_INIT *.c" % (CalculateCFLAGS(node) + CalculateUserCodeOnlyCFLAGS(node)))
                        else:
                            mysystem("\"$GNATGCC\" %s -c *.c" % (CalculateCFLAGS(node) + CalculateUserCodeOnlyCFLAGS(node)))
                        os.chdir(pwd)

            if partitionNameWithoutSuffix in CDirectories:
                for extraCdir in CDirectories[partitionNameWithoutSuffix]:
                    if len([x for x in os.listdir(extraCdir) if x.endswith(".o")])!=0:
                        externals += extraCdir + '/*.o '

            if partitionNameWithoutSuffix in AdaDirectories:
                for extraADAdir in AdaDirectories[partitionNameWithoutSuffix]:
                    if len([x for x in os.listdir(extraADAdir) if x.endswith(".o")])!=0:
                        externals += extraADAdir + '/*.o '

            if partitionNameWithoutSuffix in ExtraLibraries:
                extraLibs = ExtraLibraries[partitionNameWithoutSuffix]
                if extraLibs != []:
                    externals += ' '.join(extraLibs) + ' '

            for aplc in g_distributionNodes[node]:
                if aplc in vhdlSubsystems.keys():
                    if g_bPolyORB_HI_C:
                        # externals += ' "' + getSingleLineFromCmdOutput("echo $DMT").strip() + '/OG/libESAFPGAforC.a" '
                        externals += ' "' + getSingleLineFromCmdOutput("echo $DMT").strip() + '/ZestSC1/libZestSC1.a" /lib/i386-linux-gnu/libusb-0.1.so.4 '
                    else:
                        # externals += ' "' + getSingleLineFromCmdOutput("echo $DMT").strip() + '/OG/libESAFPGA.a" '
                        externals += ' "' + getSingleLineFromCmdOutput("echo $DMT").strip() + '/ZestSC1/libZestSC1.a" /lib/i386-linux-gnu/libusb-0.1.so.4'
                    break  # If you meet even one VHDL component for this node, the library was added to externals, no need to check further

            userCFlags += mflags(node)
            userLDFlags += mflags(node)

            if g_bPolyORB_HI_C and len(adaSubsystems) != 0:
                userLDFlags += poHiAdaLinkCmd

            # mysystem("cd '"+root+"' && cp ../../../*/*_sync.ads .")
            driversConfigPath = os.path.abspath("../DriversConfig/")
            if os.path.exists(driversConfigPath):
                driversConfigs = os.listdir(driversConfigPath)
                for dC in driversConfigs:
                    if AdaIncludePath is None:
                        AdaIncludePath = driversConfigPath + "/" + dC
                    else:
                        AdaIncludePath += ":" + driversConfigPath + "/" + dC

            if AdaIncludePath is None:
                cmd = "cd '" + root + "' && %s EXTERNAL_OBJECTS=\""
            else:
                cmd = "cd '" + root + "' && ADA_INCLUDE_PATH=\"" + AdaIncludePath + "\" %s EXTERNAL_OBJECTS=\""
            # Just before invoking ocarina-generated Makefiles, make sure that only one C_ASN1_Types.o is used:
            externalFiles = ' '.join(x for x in externals.split(' ') if not x.startswith("-"))
            os.system("rm -f `/bin/ls %s | grep C_ASN1_Types.o | sed 1d` ; exit 0" % externalFiles)

            extra = ""

            platformType = g_distributionNodesPlatform[node][0]
            if platformType.startswith("PLATFORM_X86_RTEMS"):
                os.putenv("RTEMS_MAKEFILE_PATH", os.environ["RTEMS_MAKEFILE_PATH_X86"])
            elif platformType.startswith("PLATFORM_LEON_RTEMS"):
                os.putenv("RTEMS_MAKEFILE_PATH", os.environ["RTEMS_MAKEFILE_PATH_LEON"])
            elif platformType.startswith("PLATFORM_NDS_RTEMS"):
                os.putenv("RTEMS_MAKEFILE_PATH", os.environ["RTEMS_MAKEFILE_PATH_NDS"])
            elif platformType.startswith("PLATFORM_GUMSTIX_RTEMS"):
                os.putenv("RTEMS_MAKEFILE_PATH", os.environ["RTEMS_MAKEFILE_PATH_GUMSTIX"])
            if all(x not in platformType for x in ["LEON", "RTEMS", "WIN32", "GNAT_RUNTIME"]):
                extra += "-lrt "
            if "GNAT_RUNTIME" not in platformType:
                userLDFlags += " -lm "
            if bCoverage:
                extra += " -lgcov "
            if platformType in ("PLATFORM_LINUX32",):  # and platform.architecture()[0] == '64bit':
                userCFlags += " -m32 "
                userLDFlags += " -m32 "
            if g_bPolyORB_HI_C and platformType in \
                    ("PLATFORM_LINUX32", "PLATFORM_LINUX64", "PLATFORM_X86_RTEMS", "PLATFORM_X86_LINUXTASTE", "PLATFORM_NATIVE"):
                if not bDebug:
                    userLDFlags += " -Wl,-gc-sections "
                else:
                    userCFlags += " -g "
                    userLDFlags += " -g "
            if partitionNameWithoutSuffix in g_customLDFlagsPerNode:
                userLDFlags += " " + " ".join(g_customLDFlagsPerNode[partitionNameWithoutSuffix]) + " "
            if partitionNameWithoutSuffix in g_customCFlagsPerNode:
                userCFlags += " " + " ".join(g_customCFlagsPerNode[partitionNameWithoutSuffix]) + " "
            if g_bPolyORB_HI_C and cflagsSoFar != "":
                userCFlags += " " + cflagsSoFar.replace('"', '\\"') + " "
            userCFlags = userCFlags.strip()
            if userCFlags != "":
                userCFlags = ' ' + userCFlags
            userLDFlags = userLDFlags.strip()
            if userLDFlags != "":
                userLDFlags = ' ' + userLDFlags

            if len(cppSubsystems)>0:
                userLDFlags += " -lstdc++ "

            # Workaround for bug in the new GNAT - crashes if it sees multiple -m32
            def keepOnlyFirstCompilationOption(flags):
                cmd = ""
                tokensUnique = {}
                for token in flags.split():
                    if token.startswith("-") and not token.startswith("-I"):
                        if token not in tokensUnique:
                            tokensUnique[token] = 1
                            cmd += " " + token
                    else:
                        cmd += " " + token
                return cmd + " "
            userCFlags = keepOnlyFirstCompilationOption(userCFlags)
            userLDFlags = keepOnlyFirstCompilationOption(userLDFlags)
            if "GNAT_RUNTIME" in platformType:
                userCFlags = userCFlags.replace(" -mfloat-abi=hard ", "")  # Not supported when compiling Ada
                userLDFlags = userLDFlags.replace(" -mfloat-abi=hard ", "")  # Not supported when compiling Ada
                userCFlags = userCFlags.replace("-fshort-double", "")  # Not supported when compiling Ada
                userLDFlags = userLDFlags.replace("-fshort-double", "")  # Not supported when compiling Ada
            customFlags = (' USER_CFLAGS="${USER_CFLAGS}%s" USER_LDFLAGS="${USER_LDFLAGS}%s"' % (userCFlags, userLDFlags))
            mysystem((cmd % customFlags) + extra + externals + "\" make")
    return AdaIncludePath


def GatherAllExecutableOutput(unused_outputDir, pythonSubsystems, vhdlSubsystems, tmpDirName, commentedGUIfilters, bDebug, i_aadlFile):
    '''Gathers all binaries generated (Ocarina,GUIs,Python,PeekPoke,etc) and moves them under .../binaries'''
    g_stageLog.info("Gathering all executable output")
    outputDir = g_absOutputDir
    os.chdir(outputDir)
    mkdirIfMissing(outputDir + os.sep + "/binaries")
    os.chdir("..")
    if len(vhdlSubsystems)>0:
        msg = "VHDL bit files built:"
        g_stageLog.info('-' * len(msg))
        g_stageLog.info(msg)
        cmd = "find '%s' -type f -iname '*bit'" % " ".join(vhdlSubsystems.keys())
        for line in os.popen(cmd).readlines():
            line = line.strip()
            targetFolder = outputDir + os.sep + "binaries" + os.sep
            mysystem('cp "' + line + '" "' + targetFolder + '"')
            print "        " + ColorFormatter.bold_string(targetFolder + os.path.basename(line))
    os.chdir(outputDir)
    mysystem("find '%s'/GlueAndBuild -type f -perm /111 ! -iname '*.so' -a ! -iname '*.pyd' | while read ANS ; do file \"$ANS\" | egrep 'ELF|PE32' >/dev/null 2>/dev/null && mv \"$ANS\" \"%s/binaries/\" ; done ; exit 0" % (g_absOutputDir, g_absOutputDir))
    mysystem("find '%s'/ -name binaries -prune -o -type f -perm /111 -iname '*_GUI' -exec bash -c 'F=\"{}\"; D=$(dirname \"$F\"); B=$(basename \"$F\") ; B=\"${B/_GUI/}\"; mv \"$F\" \"%s/binaries/\" ; mv \"$D\"/../../../${B}.pl \"%s/binaries/\" ; mv \"$D\"/../../../${B}_RunAndPlot.sh \"%s/binaries/\" ; ' ';' 2>/dev/null" % (g_absOutputDir, g_absOutputDir, g_absOutputDir, g_absOutputDir))
    # if len(pythonSubsystems)>0:
    #     msg = "Python bridges built under %s:" % outputDir
    #     g_stageLog.info('-' * len(msg))
    #     g_stageLog.info(msg)
    #     g_stageLog.info("Python bridges built under %s:" % outputDir)
    #     for line in os.popen("find '%s' -type f -iname PythonAccess.so -perm /111" % (g_absOutputDir)).readlines():
    #         g_stageLog.info("        Shared library: " + line.strip())

    # g_stageLog.info('-' * 70)
    # Strip binaries:
    if not bDebug:
        for n in g_distributionNodesPlatform.keys():
            if os.path.exists(outputDir + os.sep + "/binaries" + os.sep + n):
                pref = g_distributionNodesPlatform[n][1]
                mysystem("%sstrip %s" % (pref, outputDir + os.sep + "/binaries" + os.sep + n))
    if bDebug:
        g_stageLog.info("Built with debug info: you can check the stack usage of the binaries")
        g_stageLog.info("with 'checkStackUsage.py', to make sure you are within limits.")
    # ticket 224: Keep gnuplot stuff separate
    olddir = os.getcwd()
    os.chdir(outputDir + os.sep + "/binaries/")
    for i in os.listdir("."):
        if i.endswith("_GUI"):
            base = i.replace("_GUI", "")
            mysystem('mkdir -p "GnuPlot_%s"' % base)
            # These two moves may fail if the only TM/TCs carry ENUMERATED - how to plot one?!?
            mysystem('mv "%s_RunAndPlot.sh" "GnuPlot_%s"/ >/dev/null 2>&1 ; exit 0' % (base, base))
            mysystem('mv "%s.pl" "GnuPlot_%s"/  >/dev/null 2>&1 ; exit 0' % (base, base))
    os.chdir(olddir)

    # Peek and Poke section preparation
    mkdirIfMissing(outputDir + os.sep + "/binaries/PeekPoke")
    os.chdir(outputDir + os.sep + "/binaries/PeekPoke")
    for line in os.popen("find ../.. -type d -name gluetaste_probe_console").readlines():
        line = line.strip()
        mysystem('cp "%s"/python/*.py "%s"/python/*.so .' % (line, line))
        installPath = getSingleLineFromCmdOutput("taste-config --prefix")
        mysystem('cp "%s"/bin/taste-gnuplot-streams ./driveGnuPlotsStreams.pl' % installPath)
        mysystem('for i in peekpoke.py PeekPoke.glade ; do cp "%s"/share/peekpoke/$i . ; done' % installPath)
        # mysystem('echo Untaring pyinstaller.speedometer.tar.bz2... ; tar jxf "%s"/share/speedometer/pyinstaller.speedometer.tar.bz2' % installPath)
        g_stageLog.info("A PeekPoke subfolder was also created under binaries")
        g_stageLog.info("for easy run-time monitoring and control of inner variables.")
        break
    os.chdir("..")
    if [] == os.listdir("PeekPoke"):
        os.rmdir("PeekPoke")

    g_stageLog.info("Executables built under %s/binaries:" % outputDir)
    for line in os.popen(
            "find '%s'/binaries -type f -perm /111 | grep -v /PeekPoke/ ; exit 0" % (g_absOutputDir)).readlines():
        line = line.strip()
        if line.endswith('.so') or '/GUI-' in line:
            continue
        print "        " + ColorFormatter.bold_string(line.strip())

    mysystem("rm -rf \"%s\"" % tmpDirName)
    l = len(commentedGUIfilters)
    if l:
        g_stageLog.info("In the list above, the filter" + ("s: '" if l>1 else ": '") + "','".join(commentedGUIfilters) +
                        "' match" + ("es" if 1==l else "") + " only 5 of the available fields.\n"+
                        "Edit and uncomment the lines for any additional fields you need to plot.")

    if len(os.popen("find '%s/GlueAndBuild/' -type f -name guilayout.ui" % outputDir).readlines()):
        g_stageLog.info("Gathering new Python-based GUIs")

        for guiFName in os.popen("find '%s/GlueAndBuild/' -type f -name 'guilayout.ui' | sed 's,/guilayout.ui,,;s,^.*/glue,,'" % outputDir).readlines():
            if guiFName.strip().endswith('probe_console'):
                continue
            guiFName = guiFName.strip()
            stubs = {
                'gui': guiFName,
                'glue': outputDir + '/GlueAndBuild/glue' + guiFName + '/',
                'target': outputDir + "/binaries/" + guiFName + "-GUI",
                'GUItarget': outputDir + "/binaries/GUI-" + guiFName,
                'IFview': i_aadlFile,
                'DView': outputDir + "/dataview-uniq.asn"
            }
            mysystem('mkdir -p "%(target)s"' % stubs)
            mysystem('cp "%(glue)s/"*.py "%(target)s"' % stubs)
            mysystem('cp "%(glue)s/guilayout.ui" "%(target)s"' % stubs)
            mysystem('cp "%(glue)s/python/"*.py "%(target)s"' % stubs)
            mysystem('echo "errCodes = $(taste-asn1-errCodes %(glue)s/python/dataview-uniq.h)" >> "%(target)s/datamodel.py"' % stubs)
            mysystem('cp "%(glue)s/python/"*.so "%(target)s"' % stubs)
            mysystem('cp "%(glue)s/python/asn2dataModel/"*.pyd "%(target)s" 2>/dev/null || exit 0' % stubs)
            mysystem('cp "%(glue)s/python/asn2dataModel/"*.so "%(target)s" 2>/dev/null || exit 0' % stubs)
            mysystem('cp "%(glue)s/python/asn2dataModel/"DV_Types.py "%(target)s" 2>/dev/null || exit 0' % stubs)
#            mysystem('cp -r "$(taste-config --prefix)/share/speedometer/content" "%(target)s"' % stubs)
#            mysystem('cp -r "$(taste-config --prefix)/share/speedometer/dialcontrol.qml" "%(target)s"' % stubs)
            mysystem('echo \'A=$(dirname "$0") ; cd "$A/%(gui)s-GUI" && PYTHONPATH=$(taste-config --prefix)/share:$PYTHONPATH taste-gui "$@"\' > "%(GUItarget)s" && chmod +x "%(GUItarget)s"' % stubs)
#            mysystem('cp "$(taste-config --prefix)/share/asn1-editor/tasteLogo_white.png" "%(target)s"' % stubs)
            mysystem('cp "%(IFview)s" "%(target)s"/InterfaceView.aadl' % stubs)
            mysystem('cp "%(IFview)s" "%(target)s"/InterfaceView.aadl' % stubs)
            mysystem('cp "%(DView)s" "%(target)s"/' % stubs)

        for line in os.popen("find '%s'/binaries/ -maxdepth 1 -type f -perm /111 -name 'GUI*' ; exit 0" % (g_absOutputDir)):
            print "        " + ColorFormatter.bold_string(line.strip())


def CopyDatabaseFolderIfExisting():
    if os.path.isdir(g_absOutputDir + "/../sql_db"):
        for line in os.popen("find '%s'/binaries/ -maxdepth 1 -type d -iname '*GUI' ; exit 0" % (g_absOutputDir)):
            mysystem("cp -al \"%s\" \"%s\"" % (g_absOutputDir + "/../sql_db/", line.strip()))


def FixEnvVars():
    '''Updates required environment variables'''
    # DMT tarball is now obsolete - we will use the repos-provided
    # versions of the DMT tools
    DMTpath = getSingleLineFromCmdOutput("taste-config --prefix") + os.sep + "share"
    os.putenv("DMT", DMTpath)

    # ObjectGeode variables
    os.putenv("GEODE_MAPPING", "TP")
    os.putenv("GEODE_MULTI_BIN", "0")
    os.putenv("GEODE_REMOTE_CREATE", "0")
    os.putenv("GEODE_NAME_LIMIT", "30")
    os.putenv("GEODE_LINE_SIZE", "80")
    os.putenv("GEODE_STR_SIZE", "40")
    os.putenv("GEODE_ANSI_FUNCTION", "1")
    os.putenv("GEODE_PRS_HOOK", "0")
    os.putenv("GEODE_FILE_SIGNAL", "0")
    os.putenv("GEODE_FILE_PROCED", "0")
    os.putenv("GEODE_DEC_ONLINE", "0")
    os.putenv("GEODE_CVISS", "0")
    os.putenv("GEODE_FIELD_PREFIX", "fd_")
    os.putenv("GEODE_OUTPUT_FUNCTION", "0")
    os.putenv("GEODE_OUTPUT_TASK", "0")
    os.putenv("GEODE_SCHED_MODE", "0")
    os.putenv("GEODE_C_CHARSTRING", "0")
    os.putenv("GEODE_NBPAR_NODE", "0")
    os.putenv("GEODE_NBPAR_GROUP", "0")
    os.putenv("GEODE_NBPAR_TASK", "0")
    os.putenv("GEODE_NBPAR_EXTERN", "0")
    os.putenv("GEODE_NBPAR_PROC", "0")


def ParseCommandLineArgs():
    '''Parses options passed in the command line'''
    disableColor = "--nocolor" in sys.argv
    if disableColor:
        sys.argv.remove("--nocolor")
    global g_stageLog
    g_stageLog = logging.getLogger("tasteBuilder")
    console = logging.StreamHandler(sys.__stdout__)
    console.setFormatter(ColorFormatter(sys.stdin.isatty() and not disableColor))
    g_stageLog.setLevel(logging.INFO)
    g_stageLog.addHandler(console)

    g_stageLog.info("Parsing Command Line Args")
    try:
        args = sys.argv[1:]
        optlist, args = getopt.gnu_getopt(args, "fgpbrvhjn:o:s:c:i:S:M:C:B:A:G:P:V:QC:QA:e:d:l:w:x:", ['fast', 'debug', 'no-retry', 'with-polyorb-hi-c', 'with-empty-init', 'with-coverage', 'aadlv2', 'gprof', 'keep-case', 'nodeOptions=', 'output=', 'stack=', 'deploymentView=', 'interfaceView=', 'subSCADE=', 'subSIMULINK=', 'subC=', 'subCPP=', 'subAda=', 'subOG=', 'subRTDS=', 'subVHDL=', 'subQGenC=', 'subQGenAda=', 'with-extra-C-code=', 'with-extra-Ada-code=', 'with-extra-lib=', 'with-cv-attributes', '--timer='])
    except:
        usage()
    if args != []:
        usage()  # some args were not understood

    global g_bFast, g_bPolyORB_HI_C
    g_bFast = g_bPolyORB_HI_C = False
    global g_bRetry
    g_bRetry = True  # set by default
    bUseEmptyInitializers = bCoverage = bProfiling = bDebug = bKeepCase = False

    # Maxime request: never check for multicores anymore, POHI updates fixed the issues.
    enableMultiCoreCheck = False
    # if os.getenv("DISABLE_MULTICORE_CHECK") is not None:
    #     enableMultiCoreCheck = False

    outputDir = ""
    depl_aadlFile = ""
    i_aadlFile = ""
    stackOptions = ""
    cvAttributesFile = ""
    timerResolution = "100"
    scadeSubsystems = {}
    simulinkSubsystems = {}
    cSubsystems = {}
    qgencSubsystems = {}
    qgenadaSubsystems = {}
    cppSubsystems = {}
    adaSubsystems = {}
    ogSubsystems = {}
    rtdsSubsystems = {}
    vhdlSubsystems = {}
    AdaIncludePath = os.getenv("ADA_INCLUDE_PATH")
    CDirectories = {}
    AdaDirectories = {}
    ExtraLibraries = {}

    for opt, arg in optlist:
        if opt in ("-f", "--fast"):
            g_bFast = True
        elif opt in ("-g", "--debug"):
            bDebug = True
        elif opt in ("-z", "--enablemulticore"):
            enableMultiCoreCheck = False
        elif opt in ("-h", "--gprof"):
            bProfiling = True
        elif opt == "--no-retry":
            g_bRetry = False
        elif opt in ("-p", "--with-polyorb-hi-c"):
            g_bPolyORB_HI_C = True
        elif opt in ("-b", "--with-empty-init"):
            bUseEmptyInitializers = True
        elif opt in ("-r", "--with-coverage"):
            bCoverage = True
        elif opt in ("-j", "--keep-case"):
            bKeepCase = True
        elif opt in ("-o", "--output"):
            outputDir = arg
        elif opt in ("-s", "--stack"):
            stackOptions = "--stack " + str(arg) + " "
        elif opt in ("-c", "--deploymentView"):
            depl_aadlFile = arg
        elif opt in ("-i", "--interfaceView"):
            i_aadlFile = arg
        elif opt in ("-w", "--with-cv-attributes"):
            cvAttributesFile = arg
        elif opt in ("-x", "--timer"):
            timerResolution = arg
        elif opt in ("-n", "--nodeOptions"):
            subName = arg.split('@')[0]
            onOffLookup = {'on': True, 'off': False}
            for o in arg.split('@')[1:]:
                cmd, value = o.split('=')
                if value not in onOffLookup:
                    panic("Value can be '%s', not '%s' (in '%s')" %
                          ("','".join(onOffLookup.keys()), value, arg))
                value = onOffLookup[value]
                if cmd == 'debug':
                    if value:
                        g_customCFlagsPerNode.setdefault(subName, []).append("-g")
                        g_customLDFlagsPerNode.setdefault(subName, []).append("-g")
                    else:
                        g_customCFlagsPerNode.setdefault(subName, []).append("-O -DNDEBUG")
                        g_customLDFlagsPerNode.setdefault(subName, []).append("")
                elif cmd == 'gcov':
                    if value:
                        g_customCFlagsPerNode.setdefault(subName, []).append("-g -fprofile-arcs -ftest-coverage -DCOVERAGE")
                        g_customLDFlagsPerNode.setdefault(subName, []).append("-g  -fprofile-arcs -ftest-coverage -lgcov")
                elif cmd == 'gprof':
                    if value:
                        g_customCFlagsPerNode.setdefault(subName, []).append("-pg")
                        g_customLDFlagsPerNode.setdefault(subName, []).append("-pg")
                elif cmd == 'stackCheck':
                    if value:
                        g_customCFlagsPerNode.setdefault(subName, []).append("-fstack-check -fstack-protector")
                        g_customLDFlagsPerNode.setdefault(subName, []).append("-fstack-check -fstack-protector")
        elif opt in ("-S", "--subSCADE"):
            scadeSubName = arg.split(':')[0]
            if len(arg.split(':')) <= 1:
                panic('SCADE subsystems must be specified in the form subsysAadlName:zipFile')
            scadeSubsystems[scadeSubName] = arg.split(':')[1]
        elif opt in ("-M", "--subSIMULINK"):
            simulinkSubName = arg.split(':')[0]
            if len(arg.split(':')) <= 1:
                panic('SIMULINK subsystems must be specified in the form subsysAadlName:zipFile')
            simulinkSubsystems[simulinkSubName] = arg.split(':')[1]
        elif opt in ("-QC", "--subQGenC"):
            qgencSubName = arg.split(':')[0]
            if len(arg.split(':')) <= 1:
                panic('QGenC subsystems must be specified in the form subsysAadlName:zipFile')
            cSubsystems[qgencSubName] = arg.split(':')[1]
        elif opt in ("-C", "--subC"):
            cSubName = arg.split(':')[0]
            if len(arg.split(':')) <= 1:
                panic('C subsystems must be specified in the form subsysAadlName:zipFile')
            cSubsystems[cSubName] = arg.split(':')[1]
        elif opt in ("-B", "--subCPP"):
            cppSubName = arg.split(':')[0]
            if len(arg.split(':')) <= 1:
                panic('C++ subsystems must be specified in the form subsysAadlName:zipFile')
            cppSubsystems[cppSubName] = arg.split(':')[1]
        elif opt in ("-A", "--subAda"):
            adaSubName = arg.split(':')[0]
            if len(arg.split(':')) <= 1:
                panic('Ada subsystems must be specified in the form subsysAadlName:zipFile')
            adaSubsystems[adaSubName] = arg.split(':')[1]
        elif opt in ("-QA", "--subQGenAda"):
            adaSubName = arg.split(':')[0]
            if len(arg.split(':')) <= 1:
                panic('QGenAda subsystems must be specified in the form subsysAadlName:zipFile')
            qgenadaSubsystems[adaSubName] = arg.split(':')[1]
            adaSubsystems[adaSubName] = arg.split(':')[1]
        elif opt in ("-G", "--subOG"):
            ogSubName = arg.split(':')[0]
            if len(arg.split(':')) <= 1:
                panic('ObjectGeode subsystems must be specified in the form subsysAadlName:file1.pr<,file2.pr,..>')
            ogSubsystems.setdefault(ogSubName, []).extend(arg.split(':')[1].split(","))
        elif opt in ("-P", "--subRTDS"):
            rtdsSubName = arg.split(':')[0]
            if len(arg.split(':')) <= 1:
                panic('RTDS subsystems must be specified in the form subsysAadlName:zipFile')
            rtdsSubsystems[rtdsSubName] = arg.split(':')[1]
        elif opt in ("-V", "--subVHDL"):
            vhdlSubsystems[arg] = 1
        elif opt in ("-e", "--with-extra-C-code"):
            try:
                partition, extraCdir = arg.split(':')
                if not os.path.exists(extraCdir) or not os.path.isdir(extraCdir):
                    panic("Can't find directory with extra C code: '%s'" % extraCdir)
                extraCdir = os.path.abspath(extraCdir)
                CDirectories.setdefault(partition, []).append(extraCdir)
            except:
                panic("Invalid argument to -e (%s) - must be <deploymentPartition:directoryWithCfiles>" % arg)
        elif opt in ("-d", "--with-extra-Ada-code"):
            try:
                partition, extraADAdir = arg.split(':')
                if not os.path.exists(extraADAdir) or not os.path.isdir(extraADAdir):
                    panic("Can't find directory with extra Ada code: '%s'" % extraADAdir)
                extraADAdir = os.path.abspath(extraADAdir)
                AdaDirectories.setdefault(partition, []).append(extraADAdir)
                if AdaIncludePath is not None:
                    AdaIncludePath += ":" + extraADAdir
                else:
                    AdaIncludePath = extraADAdir
                os.putenv("ADA_INCLUDE_PATH", AdaIncludePath)
            except:
                panic("Invalid argument to -d (%s) - must be <deploymentPartition:directoryWithADBfiles>" % arg)
        elif opt in ("-l", "--with-extra-lib"):
            try:
                partition, extraLibs = arg.split(":")
                extraLibs = extraLibs.split(",")
                for idx, l in enumerate(extraLibs[:]):
                    if not os.path.exists(l) or not (os.path.isfile(l) or os.path.islink(l)):
                        panic("Can't find file or symlink for library '%s'" % l)
                    extraLibs[idx] = os.path.abspath(l)
                ExtraLibraries.setdefault(partition, []).extend(copy.deepcopy(extraLibs))
            except:
                panic("Invalid argument to -l (%s) - must be: deploymentPartition:/path/to/libLibrary1.a<,/path/to/libLibrary2.a,...>" % arg)

    if outputDir == "" or depl_aadlFile == "" or i_aadlFile == "":
        usage()
    if enableMultiCoreCheck and DetermineNumberOfCPUs() > 1:
        panic("""
VMWARE has timing issues when emulating multi-core CPUs, you MUST change
your .vmx to only use 1 core in this virtual machine. If you intend to use
this machine simply to build a binary - but NOT to run it, then you can
disable this check with the -z command line argument or by setting
"DISABLE_MULTICORE_CHECK" in your environment.
""")

    global g_absOutputDir
    g_absOutputDir = os.path.abspath(outputDir)
    mkdirIfMissing(outputDir)

    # Initial log entry
    mysystem("date", outputDir=g_absOutputDir)

    # Removed check for bash, 2011/Apr/8 : all bashisms are gone now (I think)
    # banner("Checking for valid shell")
    # mysystem("/bin/sh --version 2>/dev/null | grep bash >/dev/null || { echo Your /bin/sh is not bash ... aborting... ; exit 1 ; }")

    filesMustExist = [depl_aadlFile, i_aadlFile]
    if cvAttributesFile != "":
        filesMustExist.append(cvAttributesFile)
    for f in filesMustExist:
        if not os.path.exists(f):
            panic("'%s' doesn't exist!" % f)

    os.putenv("ASN1SCC", getSingleLineFromCmdOutput("echo $DMT").strip() + "/asn1scc/asn1.exe")

    def spawnOcarinaFailed(v):
        return 0 == len(os.popen("ocarina " + v + " 2>&1 | grep ^Ocarina").readlines())
    if all(spawnOcarinaFailed(arg) for arg in ["-V", "--version"]):
        panic("Your PATH has no 'ocarina' !")

    # We set LANG to C to avoid issues with LOCALES
    os.putenv("LANG", "C")

    for d in [scadeSubsystems, simulinkSubsystems, cSubsystems, cppSubsystems, adaSubsystems, rtdsSubsystems]:
        for i in d.keys():
            if not os.path.exists(d[i]):
                panic("'%s' doesn't exist!" % d[i])
            d[i] = os.path.abspath(d[i])
    for name, prFiles in ogSubsystems.items():
        for j in xrange(0, len(prFiles)):
            if not os.path.exists(ogSubsystems[name][j]):
                panic("'%s' doesn't exist!" % ogSubsystems[name][j])
            ogSubsystems[name][j] = os.path.abspath(ogSubsystems[name][j])

    return (outputDir, i_aadlFile, depl_aadlFile,
            bDebug, bProfiling, bUseEmptyInitializers, bCoverage, bKeepCase, cvAttributesFile,
            stackOptions, AdaIncludePath, AdaDirectories, CDirectories, ExtraLibraries,
            scadeSubsystems, simulinkSubsystems, qgencSubsystems, qgenadaSubsystems, cSubsystems,
            cppSubsystems, adaSubsystems, rtdsSubsystems, ogSubsystems, vhdlSubsystems,
            timerResolution)


def ReadMD5sums(bDebug):
    '''Opens the MD5 hashes database (used to avoid working on the same input needlessly)'''
    md5s = {}
    md5hashesFilename = "md5hashes" + ("Debug" if bDebug else "")
    if os.path.exists(md5hashesFilename):
        for line in open(md5hashesFilename, 'r').readlines():
            md5s[line.split(':')[0]] = line.split(':')[1].strip()
    return md5s, md5hashesFilename


def CreateDataViews(i_aadlFile, asn1Grammar, acnFile, baseASN, md5s, md5hashesFilename):
    '''Invokes asn2aadlPlus to create AADL DataViews'''
    g_stageLog.info("Creating AADL dataviews")
    # Create a "cropped" version of the input ASN.1 grammar, one without the TASTE directives
    mysystem("\"$DMT/asn1scc/taste-extract-asn-from-design.exe\" -i \"%s\" -k \"%s\" -c \"%s\"" % (i_aadlFile, asn1Grammar, acnFile))
    mysystem("cp \"" + asn1Grammar + "\" . 2>/dev/null || exit 0")
    mysystem("cp \"" + acnFile + "\" . 2>/dev/null || exit 0")
    if os.path.getsize(acnFile):
        mysystem("cp \"" + acnFile + "\" . 2>/dev/null || exit 0")
        acnFile = os.path.abspath(os.path.basename(acnFile))
    else:
        # from the "cropped" version, create the default .ACN
        oldBaseACN = os.path.basename(acnFile)
        if os.path.exists(oldBaseACN):
            os.unlink(oldBaseACN)
        mysystem("mono \"$DMT\"/asn1scc/asn1.exe -ACND \"" + baseASN + "\"")
        acnFile = os.path.abspath(baseASN.replace(".asn", ".acn"))

    # Now create the full (non-cropped) ASN.1 grammar, since the DataView AADL we will create below must include ALL types
    # (i.e. including TASTE-Directives)
    mysystem("\"$DMT/asn1scc/taste-extract-asn-from-design.exe\" -i \"%s\" -j \"%s\"" % (i_aadlFile, asn1Grammar))

    # Create the DataView AADL
    newGrammar = False
    if asn1Grammar not in md5s or (acnFile not in md5s) or \
            md5s[asn1Grammar]!=md5hash(asn1Grammar) or md5s[acnFile]!=md5hash(acnFile):
        mysystem("asn2aadlPlus -acn \"" + acnFile + "\" \"" + asn1Grammar + "\" D_view.aadl")
        md = open(g_absOutputDir + os.sep + md5hashesFilename, 'a')
        md.write("%s:%s\n" % (asn1Grammar, md5hash(asn1Grammar)))
        md.write("%s:%s\n" % (acnFile, md5hash(acnFile)))
        md.close()
        newGrammar = True
    else:
        print "No need to rebuild AADLv1 DataView"
        sys.stdout.flush()

    if asn1Grammar + "_aadlv2" not in md5s or (acnFile not in md5s) or \
            md5s[asn1Grammar + "_aadlv2"]!=md5hash(asn1Grammar) or md5s[acnFile]!=md5hash(acnFile):
        mysystem("asn2aadlPlus -aadlv2  -acn \"" + acnFile + "\" \"" + asn1Grammar + "\" D_view_aadlv2.aadl")
        md = open(g_absOutputDir + os.sep + md5hashesFilename, 'a')
        md.write("%s:%s\n" % (asn1Grammar + "_aadlv2", md5hash(asn1Grammar)))
        md.write("%s:%s\n" % (acnFile, md5hash(acnFile)))
        md.close()
    else:
        print "No need to rebuild AADLv2 DataView"
        sys.stdout.flush()

    # And now, re-create the "cropped" version of the input ASN.1 grammar, that everyone else uses
    mysystem("\"$DMT/asn1scc/taste-extract-asn-from-design.exe\" -i \"%s\" -k \"%s\"" % (i_aadlFile, asn1Grammar))
    return acnFile, newGrammar


def InvokeASN1Compiler(asn1Grammar, baseASN, acnFile, baseACN, isNewGrammar, bCoverage):
    '''Invokes the ASN.1 compiler and the msgPrinter code generator'''
    g_stageLog.info("Invoking ASN1 Compiler")
    mkdirIfMissing('auto-src')
    os.chdir('auto-src')

    # Copy files that are potentially used by all partitions
    mysystem("cp \"$DMT\"/AutoGUI/debug_messages.? .")
    mysystem("cp \"$DMT\"/AutoGUI/timeInMS.? .")

    # Copy ASN.1 and ACN grammars
    mysystem('cp "' + asn1Grammar + '" .')
    mysystem('cp "' + acnFile + '" .')

    # Invoke compiler
    if isNewGrammar:
        if bCoverage:
            mysystem("mono \"$DMT\"/asn1scc/asn1.exe -c -uPER -typePrefix asn1Scc -noInit -noChecks -wordSize 8 -ACN \"" + baseACN + "\" \"" + baseASN + "\"")
        else:
            mysystem("mono \"$DMT\"/asn1scc/asn1.exe -c -uPER -typePrefix asn1Scc -wordSize 8 -ACN \"" + baseACN + "\" \"" + baseASN + "\"")
    else:
        print "No need to reinvoke the ASN.1 compiler"
        sys.stdout.flush()

    # Create message printers, for use when displaying inner messages with MSCs
    mysystem('msgPrinter "' + baseASN + '"')

    # Create message printers for ASN.1 variables, for use when sending messages for MSCs
    mysystem('msgPrinterASN1 "' + baseASN + '"')

    os.chdir('../')


def UnzipSCADEcode(scadeSubsystems):
    '''Unpacks and fixes up SCADE code'''
    if scadeSubsystems:
        g_stageLog.info("Unziping SCADE code")
    for baseDir, ss in scadeSubsystems.items():
        mkdirIfMissing(baseDir)
        os.chdir(baseDir)
        if not ss.lower().endswith(".zip"):
            panic("Only .zip files supported for SCADE...")
        mysystem("unzip -o \"" + ss + "\"")
        if not os.path.isdir(baseDir):
            panic("Zip file '%s' must contain a directory with the same name as the AADL subsystem name (%s)\n" % (ss, baseDir))
        mysystem("find \"%s\"/ ! -type d -exec chmod -x '{}' ';'" % baseDir)
        mysystem("find \"%s\"/ -exec touch '{}' ';'" % baseDir)
        mysystem("find \"%s\"/ -type f -iname '*.o' -exec rm -f '{}' ';'" % baseDir)
        for i in ['xer.c', 'ber.c', 'real.c', 'asn1crt.c', 'and', 'acn.c']:
            mysystem("find \"%s\"/ -type f -iname %s -exec rm -f '{}' ';'" % (baseDir, i))
        os.chdir("..")


def UnzipSimulinkCode(simulinkSubsystems):
    '''Unpacks and fixes up Simulink code'''
    if simulinkSubsystems:
        g_stageLog.info("Unziping Simulink code")
    majorSimulinkVersion = "7"
    bUseSimulinkMakefiles = {}
    for baseDir, ss in simulinkSubsystems.items():
        mkdirIfMissing(baseDir)
        os.chdir(baseDir)
        if not ss.lower().endswith(".zip"):
            panic("Only .zip files supported for SIMULINK...")
        mysystem("unzip -o \"" + ss + "\"")
        if not os.path.isdir(baseDir):
            panic("Zip file '%s' must contain a directory with the same name as the AADL subsystem name (%s)\n" % (ss, baseDir))
        mysystem("find \"%s\"/ ! -type d -exec chmod -x '{}' ';'" % baseDir)
        mysystem("find \"%s\"/ -exec touch '{}' ';'" % baseDir)
        mysystem("find \"%s\"/ -type f -iname '*.o' -exec rm -f '{}' ';'" % baseDir)
        for i in ['xer.c', 'ber.c', 'real.c', 'asn1crt.c', 'and', 'acn.c']:
            mysystem("find \"%s\"/ -type f -iname %s -exec rm -f '{}' ';'" % (baseDir, i))
        # Remove the .c file with the main function
        for line in os.popen("grep 'main(' */*main.c /dev/null | grep -v directives/ | sed 's,:.*,,'", 'r').readlines():
            line = line.strip()
            majorSimulinkVersion = (os.popen('grep R20 "' + line + '"' + " | sed 's,^.*: \([0-9]\).*R20.*$,\\1,'", 'r').readlines())[0].strip()
            print "Detected Simulink major version:", majorSimulinkVersion
            sys.stdout.flush()
            os.unlink(line)
            break
        # Fixup the makefile (if present) and decide whether to use a makefile or not
        makefiles = [x for x in os.listdir(baseDir) if (x.endswith(".mk") and x!="unixtools.mk")]
        pattern1 = re.compile(r'BUILDARGS.*OPTS="([^"]*)"')
        pattern2 = re.compile(r'^OPTS\s*=(.*)$')
        pattern3 = re.compile(r'^include .*/unixtools.mk')
        cflags = ""
        os.chdir(baseDir)
        if len(makefiles)==1:
            bUseSimulinkMakefiles[baseDir] = [True, makefiles[0], ""]
            shutil.copy(makefiles[0], makefiles[0] + ".original")
            f = open(makefiles[0], "w")
            for line in open(makefiles[0] + ".original").readlines():
                line = re.sub(r'(:.*?)(\w+\.tmw)', '\\1', line)
                buildargs = re.match(pattern1, line)
                if buildargs:
                    bUseSimulinkMakefiles[baseDir][2] = cflags = buildargs.group(1)
                opts = re.match(pattern2, line)
                if opts:
                    line = "OPTS = " + cflags + " " + opts.group(1)
                if re.match(pattern3, line):
                    line = 'include unixtools.mk'
                if line.startswith("$(OBJS) : $(MAKEFILE)"):
                    line = "$(OBJS) : $(MAKEFILE)\n\nassertBuild: $(OBJS)\n\n"
                f.write(line.replace('-fPIC', ''))
            f.close()
        elif len(makefiles)>1:
            panic("For %s: more than one makefiles inside the package (%s)" % (ss, str(makefiles)))
        else:
            bUseSimulinkMakefiles[baseDir] = [False, "", ""]
        os.chdir("../..")
    return majorSimulinkVersion, bUseSimulinkMakefiles


def UnzipCcode(subsystems, lang='C'):
    '''Unpacks and fixes up C code'''
    if subsystems:
        g_stageLog.info("Unziping %s code" % lang)
    for baseDir, ss in subsystems.items():
        mkdirIfMissing(baseDir)
        os.chdir(baseDir)
        if not ss.lower().endswith(".zip"):
            panic("Only .zip files supported for %s code..." % lang)
        mysystem("unzip -o \"" + ss + "\"")
        if not os.path.isdir(baseDir):
            panic("%s Zip file '%s' must contain a directory with the same name as the AADL subsystem name (%s)\n" %
                  (lang, ss, baseDir))
        extension = '.c' if lang == 'C' else '.cc'
        if 0 == len([x for x in os.listdir(baseDir) if x.endswith(extension)]):
            panic("%s Zip file '%s' must contain a directory with at least one %s file!\n" % (lang, ss, extension))
        mysystem("find \"%s\"/ ! -type d -exec chmod -x '{}' ';'" % baseDir)
        mysystem("find \"%s\"/ -exec touch '{}' ';'" % baseDir)
        mysystem("find \"%s\"/ -type f -iname '*.o' -exec rm -f '{}' ';'" % baseDir)
        for i in ['xer.c', 'ber.c', 'real.c', 'asn1crt.c', 'and', 'acn.c']:
            mysystem("find \"%s\"/ -type f -iname %s -exec rm -f '{}' ';'" % (baseDir, i))
        os.chdir("..")


def UnzipAdaCode(adaSubsystems, AdaIncludePath):
    '''Unpacks and fixes up Ada code'''
    if adaSubsystems:
        g_stageLog.info("Unziping Ada code")
    for baseDir, ss in adaSubsystems.items():
        mkdirIfMissing(baseDir)
        os.chdir(baseDir)
        functionalCodeDir = os.path.abspath(os.getcwd())
        if not ss.lower().endswith(".zip"):
            panic("Only .zip files supported for Ada code...")
        mysystem("unzip -o \"" + ss + "\"")
        if not os.path.isdir(baseDir):
            panic("Ada Zip file '%s' must contain a directory with the same name as the AADL subsystem name (%s)\n" % (ss, baseDir))
        if 0 == len([x for x in os.listdir(baseDir) if x.endswith(".adb")]):
            panic("Ada Zip file '%s' must contain a directory with at least one .adb file!\n" % ss)
        for root, _, files in os.walk(baseDir):
            for name in files:
                if name.lower().endswith(".adb") or name.lower().endswith(".ads"):
                    if 0 != len([c for c in name if c.isupper()]):
                        # panic("Ada user code must be in lower case filenames! (%s)" % name)
                        os.rename(root + os.sep + name, root + os.sep + name.lower())
        mysystem("find \"%s\"/ ! -type d -exec chmod -x '{}' ';'" % baseDir)
        mysystem("find \"%s\"/ -exec touch '{}' ';'" % baseDir)
        mysystem("find \"%s\"/ -type f -iname '*.o' -exec rm -f '{}' ';'" % baseDir)
        if AdaIncludePath is not None:
            AdaIncludePath += ":" + functionalCodeDir + os.sep + baseDir
        else:
            AdaIncludePath = functionalCodeDir + os.sep + baseDir
        os.putenv("ADA_INCLUDE_PATH", AdaIncludePath)
        os.chdir("..")
    return AdaIncludePath


def UnzipRTDS(rtdsSubsystems):
    '''Unpacks and fixes up PragmaDev RTDS code'''
    if rtdsSubsystems:
        g_stageLog.info("Unziping RTDS")
    for baseDir, ss in rtdsSubsystems.items():
        mkdirIfMissing(baseDir)
        os.chdir(baseDir)
        if not ss.lower().endswith(".zip"):
            panic("Only .zip files supported for RTDS...")
        mysystem("unzip -o \"" + ss + "\"")
        if not os.path.isdir(baseDir):
            panic("Zip file '%s' must contain a directory with the same name as the AADL subsystem name (%s)\n" % (ss, baseDir))
        mysystem("find \"%s\"/ ! -type d -exec chmod -x '{}' ';'" % baseDir)
        mysystem("find \"%s\"/ -exec touch '{}' ';'" % baseDir)
        mysystem("find \"%s\"/ -type f -iname '*.o' -exec rm -f '{}' ';'" % baseDir)
        for i in ['xer.c', 'ber.c', 'real.c', 'asn1crt.c', 'and', 'acn.c']:
            mysystem("find \"%s\"/ -type f -iname %s -exec rm -f '{}' ';'" % (baseDir, i))
        os.chdir("..")


def DetectAdaPackages(adaSubsystems, asn1Grammar):
    '''Uses a special mode in the ASN1 compiler that identifies Ada packages'''
    if adaSubsystems:
        g_stageLog.info("Detecting Ada Packages")
    uniqueSetOfAdaPackages = {"adaasn1rtl": 1}
    if adaSubsystems:
        for l in os.popen("mono \"$DMT\"/asn1scc/asn1.exe -AdaUses \"%s\"" % asn1Grammar).readlines():
            uniqueSetOfAdaPackages[l.split(':')[1].rstrip().lower()]=1
    return uniqueSetOfAdaPackages


def InvokeBuildSupport(i_aadlFile, depl_aadlFile, bKeepCase, bDebug, stackOptions, cvAttributesFile, timerResolution):
    '''Invokes the buildsupport code generator that creates the PI/RI bridges'''
    g_stageLog.info("Invoking BuildSupport")
    caseHandling = " --keep-case " if bKeepCase else ""
    installPath = getSingleLineFromCmdOutput("taste-config --prefix")
    ellidissLibs = ("{inst}/share/config_ellidiss/TASTE_IV_Properties.aadl"
                    " {inst}/share/config_ellidiss/TASTE_DV_Properties.aadl"
                    .format(inst=installPath))
    dv = "D_view_aadlv2.aadl"
    dbgOption = " -g " if bDebug else ""
    shutil.copy(depl_aadlFile, ".")
    mysystem("cp $(ocarina-config --resources)/AADLv2/ocarina_components.aadl .")
    mysystem('cleanupDV.pl "%s" > a_temp_name && mv a_temp_name "%s"' % (os.path.basename(depl_aadlFile), os.path.basename(depl_aadlFile)))
    converterFlag = ""
    if not any('Taste::version' in x for x in open(depl_aadlFile).readlines()):
        converterFlag = " --future "
    timerOption = " -x " + timerResolution + " "
    if g_bPolyORB_HI_C:
        mysystem('"buildsupport" ' + timerOption + converterFlag + dbgOption + stackOptions + caseHandling + ' --gw --glue -i "' + i_aadlFile + '" ' + ' -c "' + os.path.basename(depl_aadlFile) + '" ocarina_components.aadl ' + " -d " + dv + " --polyorb-hi-c --smp2 " + ellidissLibs)
    else:
        mysystem('"buildsupport" ' + timerOption + converterFlag + dbgOption + stackOptions + caseHandling + ' --gw --glue -i "' + i_aadlFile + '" ' + ' -c "' + os.path.basename(depl_aadlFile) + '" ocarina_components.aadl ' + " -d " + dv + " --smp2 " + ellidissLibs)
    if cvAttributesFile != "":
        # processList = ['ConcurrencyView/process.aadl']
        processList = glob.glob("ConcurrencyView/*_Thread.aadl")
        # processList.append(os.popen("ocarina-config --resources").readlines()[0].strip() + "/AADLv2/ocarina_components.aadl")
        g_stageLog.info("Updating thread priorities, stack sizes, and phases using " + os.path.basename(cvAttributesFile) + " as input")
        mysystem(
            "TASTE-CV --edit-aadl " +
            ",".join('"' + x + '"' for x in processList) +
            " --update-properties " +
            '"' + cvAttributesFile + '" --show false')


def AdaSpecialHandling(AdaIncludePath, adaSubsystems):
    '''Adds the backdoor APIs requested by TERMA, and fixes ADA_INCLUDE_PATH'''
    # Prepare Ada subsystems for ADA_INCLUDE_PATH based compilation (gnatmake -x)
    for baseDir in adaSubsystems.keys():
        os.chdir(baseDir)
        mysystem("rm -f \"" + baseDir + ".adb\"")
        for x in os.listdir("."):
            if x.endswith('.ads') or x.endswith('.adb'):
                mysystem("mv \"%s\" \"%s\"" % (x, baseDir))
        os.chdir("..")

    for maybeDir in os.listdir("."):
        if not os.path.isdir(maybeDir):
            continue
        if not maybeDir.startswith("fv_") and not maybeDir.startswith("vt_"):
            continue
        if AdaIncludePath is not None:
            AdaIncludePath += ":" + os.path.abspath("." + os.sep + maybeDir)
        else:
            AdaIncludePath = os.path.abspath("." + os.sep + maybeDir)
        os.putenv("ADA_INCLUDE_PATH", AdaIncludePath)
    return AdaIncludePath


def ParsePartitionInformation():
    '''Parses the 'nodes' output of buildsupport to learn about the system's node(s)'''
    g_stageLog.info("Parsing Partition Information")
    global g_distributionNodes
    g_distributionNodes = {}
    global g_distributionNodesPlatform
    g_distributionNodesPlatform = {}
    partitionName = ""
    existingPartitionNamesWithoutSuffix = []
    for line in open("ConcurrencyView/nodes").readlines():
        line = line.strip()
        if line == "" or line.startswith("--"):
            continue
        if line.startswith("*"):
            data = line.split()  # e.g. ['*', 'mypartition_obj142', 'PLATFORM_LEON_RTEMS']
            partitionName = data[1]
            partitionNameWithoutSuffix = re.sub(r'_obj\d+$', '', partitionName)
            if partitionNameWithoutSuffix in existingPartitionNamesWithoutSuffix:
                panic("\nYou can't use two partitions with the same name (%s)!" % partitionNameWithoutSuffix)
            existingPartitionNamesWithoutSuffix.append(partitionNameWithoutSuffix)
            if 'RTEMS' in data[2]:
                SetEnvForRTEMS(data[2])
            g_distributionNodes[partitionName] = []
            # New detection logic for platform-level CC, CFLAGS and LDFLAGS to use
            # (from ticket 311)
            makefilename = "/tmp/Makefile" + str(os.getpid())
            f = open(makefilename, "w")
            f.write('include GlueAndBuild/deploymentview_final/' + partitionName + '/Makefile\n')
            f.write('\n')
            f.write('printCC:\n')
            f.write('\t@$(info $(CC))\n\n')
            f.write('printCflags:\n')
            f.write('\t@$(info $(CFLAGS))\n\n')
            f.write('printLdflags:\n')
            f.write('\t@$(info $(LDFLAGS))\n\n')
            f.close()
            try:
                cc = getSingleLineFromCmdOutput("make -s -f " + makefilename + " printCC 2>&1").split()[0]
                if cc == "cc":
                    prefix = ""
                else:
                    prefix = re.sub(r'gcc$', '', cc)
            except:
                panic("Failed to detect a proper compiler for " + partitionName)
            cf = getSingleLineFromCmdOutput("make -s -f " + makefilename + " printCflags 2>&1")
            cf = cf.replace("-DRTEMS_PURE", "")
            ld = getSingleLineFromCmdOutput("make -s -f " + makefilename + " printLdflags 2>&1")
            os.unlink(makefilename)
            if partitionNameWithoutSuffix not in g_customCFlagsForUserCodeOnlyPerNode:
                g_customCFlagsForUserCodeOnlyPerNode.setdefault(partitionNameWithoutSuffix, []).append(cf)
            if partitionNameWithoutSuffix not in g_customLDFlagsPerNode:
                g_customLDFlagsPerNode.setdefault(partitionNameWithoutSuffix, []).append(ld)
            g_log.write('for ' + partitionNameWithoutSuffix + ', identified CC:\n' + cc + '\n')
            g_log.write('for ' + partitionNameWithoutSuffix + ', identified CFLAGS:\n' + cf + '\n')
            g_log.write('for ' + partitionNameWithoutSuffix + ', identified LDFLAGS:\n' + ld + '\n')
            g_distributionNodesPlatform[partitionName] = [data[2], prefix]
            try:
                if 'coverage' in data[3:]:
                    g_customCFlagsPerNode.setdefault(partitionName, []).append("-g -fprofile-arcs -ftest-coverage -DCOVERAGE")
                    g_customLDFlagsPerNode.setdefault(partitionName, []).append("-g  -fprofile-arcs -ftest-coverage -lgcov")
            except:
                pass
        else:
            g_fromFunctionToPartition[line] = partitionNameWithoutSuffix
            if line not in g_distributionNodes[partitionName]:
                g_distributionNodes[partitionName].append(line)
                g_distributionNodesPlatform[line] = [data[2], prefix]


def FindWrappers():
    '''Identifies the wrappers generated by buildsupport'''
    g_stageLog.info("Finding Wrappers")
    wrappers = []
    for line in os.popen("/bin/ls */*/*wrappers.ad? */*wrappers.ad? 2>/dev/null", 'r').readlines():
        line = line.strip()
        wrappers.append(os.path.abspath(line))
    return wrappers


def DetectGUIsubSystems(AdaIncludePath):
    '''Detects the GUI systems that will be built'''
    g_stageLog.info("Detecting GUI subSystems")
    guiSubsystems = []
    for line in os.popen("/bin/ls */*gui_code.c 2>/dev/null", 'r').readlines():
        line = line.strip()
        baseDir = re.sub(r'/.*', '', line)
        if not os.path.exists(baseDir + os.sep + "mini_cv.aadl"):
            panic("'%s' appears to contain a GUI, but no 'mini_cv.aadl' is inside..." % baseDir)
        guiSubsystems.append(baseDir)
        if AdaIncludePath is not None:
            AdaIncludePath += ":" + os.path.abspath(baseDir)
        else:
            AdaIncludePath = os.path.abspath(baseDir)
        os.putenv("ADA_INCLUDE_PATH", AdaIncludePath)
    return guiSubsystems, AdaIncludePath


def DetectCyclicSubsystems():
    '''Detects the Cyclic systems that will be built'''
    g_stageLog.info("Detecting Cyclic subsystems")
    cyclicSubsystems = []
    for line in os.popen("/bin/ls */*_hook 2>/dev/null", 'r').readlines():
        line = line.strip()
        baseDir = re.sub(r'/.*', '', line)
        cyclicSubsystems.append(baseDir)
    return cyclicSubsystems


def InvokeObjectGeodeGenerator(ogSubsystems):
    '''Invokes the ObjectGeode code generator'''
    if ogSubsystems:
        g_stageLog.info("Invoking ObjectGeode Generator")
    for baseDir in ogSubsystems.keys():
        if not os.path.exists(baseDir):
            panic('buildsupport did not generate directory "%s"' % baseDir)
        os.chdir(baseDir)
        if len(ogSubsystems[baseDir]) == 0:
            panic("At least one .pr file must be specified! (%s)" % str(baseDir))
        mkdirIfMissing("ext")
        os.chdir("ext")
        for f in ogSubsystems[baseDir]:
            mysystem("cp \"" + f + "\" .")
        mysystem("wine \"$DMT\\OG\\sdl2c.exe\" " + " ".join(['"' + os.path.basename(x) + '"' for x in ogSubsystems[baseDir]]) + " -ts -info -parse")
        mysystem("cp \"$DMT\"/OG/g2_*.h  \"$DMT\"/OG/g2_*.c  .")
        os.chdir("..")  # out of ext/
        # os.system('wine "$DMT/OG/build_SDL_glue.exe" ' + ' '.join(['"ext\\'+os.path.basename(x)+'"' for x in baseDir]))
        mysystem("cp *.[ch] ext/")
        mysystem("cp \"$DMT\"/OG/sdl_main.c ext/")
        mysystem("cp \"$DMT\"/OG/g2_btstr.c ext/")
        mysystem("rm -f ext/n_*.c")
        os.chdir("..")


def CreateAndCompileGlue(
    asn1Grammar, cflagsSoFar,
        scadeIncludes, simulinkIncludes, cIncludes, adaIncludes, rtdsIncludes, guiIncludes, cyclicIncludes,
        scadeSubsystems, simulinkSubsystems, cSubsystems, cppSubsystems, adaSubsystems, rtdsSubsystems, ogSubsystems, guiSubsystems, cyclicSubsystems, vhdlSubsystems,
        md5s, md5hashesFilename,
        majorSimulinkVersion, bUseSimulinkMakefiles):

    '''Invokes aadl2glueC to create the glue code, and compiles it'''
    g_stageLog.info("Creating and compiling glue code")

    mkdirIfMissing("GlueAndBuild")
    os.chdir("GlueAndBuild")
    mysystem("cp \"" + asn1Grammar + "\" .")

    def InvokeAadl2GlueCandCompile(baseDir, lock):
        # With AADLv2, we may have multi-platform builds
        if (baseDir in g_distributionNodesPlatform.keys()):
            UpdateEnvForNode(baseDir)

        mkdirIfMissing("glue" + baseDir)

        lock.acquire()
        print "Creating any possible glue for", baseDir, "- and compiling it"
        sys.stdout.flush()
        lock.release()

        absDview = os.path.abspath('../D_view.aadl')
        absMinicv = os.path.abspath('../' + baseDir + '/mini_cv.aadl')
        if os.getenv("ZESTSC1") is not None:
            vhdlIncludes = "-I ~/work/Xilinx/ZestSC1/Inc/ "
        else:
            vhdlIncludes = " "
        if absDview not in md5s or md5s[absDview] != md5hash(absDview) or absMinicv not in md5s or md5s[absMinicv] != md5hash(absMinicv):
            mysystem("aadl2glueC -o \"glue" + baseDir + "\" ../D_view.aadl \"../" + baseDir + "/mini_cv.aadl\"")

            if 0 == len([x for x in os.listdir("glue" + baseDir) if x.endswith(".c") or x.endswith(".h")]):
                return
            os.chdir("glue" + baseDir)
            # Before you compile the glue, use the detected Simulink version to "hack"
            # the difference between RTW7 and RTW6 in the initialization
            if baseDir in simulinkSubsystems.keys():
                # Learn about Simulink TASTE-Directives, because the glue compilation may depend on them
                curDir = os.path.abspath(os.getcwd())
                os.chdir("../../" + baseDir + os.sep + baseDir)
                CheckDirectives(baseDir)
                os.chdir(curDir)
                # Patch calls to _initiliaze functions, versions >7 don't pass anything
                if int(majorSimulinkVersion) >= 7:
                    mysystem("for i in *.c ; do cat \"$i\" | sed 's,_initialize(1),_initialize(),' > a_temp_name ; mv a_temp_name \"$i\" ; done")
                # Patch calls to _step functions, sometimes they have 0 param, sometimes they don't
                # so look at the header files...
                mysystem('LINES=`grep "_step.*int_T.*tid" ../../"%s"/"%s"/*h  2>/dev/null | wc -l` ; if [ $LINES -eq 1 ] ; then for i in *.c ; do cat "$i" | sed "s,_step(),_step(0)," > a_temp_name && mv a_temp_name "$i" ; done ; fi ; exit 0' % (baseDir, baseDir))
                mysystem("\"$GNATGCC\" -c %s -I ../../auto-src %s %s %s %s %s %s %s %s *.c" % (
                    cflagsSoFar + CalculateCFLAGS(baseDir, withPOHIC=False) + CalculateUserCodeOnlyCFLAGS(baseDir),
                    bUseSimulinkMakefiles[baseDir][2],
                    scadeIncludes, simulinkIncludes, cIncludes, guiIncludes, adaIncludes, cyclicIncludes, rtdsIncludes))
            else:
                mysystem("\"$GNATGCC\" -c %s -I ../../auto-src %s %s %s %s %s %s %s %s *.c" % (
                    cflagsSoFar + CalculateCFLAGS(baseDir, withPOHIC=False) + CalculateUserCodeOnlyCFLAGS(baseDir),
                    scadeIncludes, simulinkIncludes, cIncludes, guiIncludes, adaIncludes, cyclicIncludes, rtdsIncludes,
                    vhdlIncludes))
            os.chdir("..")

            lock.acquire()
            md = open(g_absOutputDir + os.sep + md5hashesFilename, 'a')
            for i in [absDview, absMinicv]:
                md.write("%s:%s\n" % (i, md5hash(i)))
            md.close()
            lock.release()
        else:
            lock.acquire()
            print "No need to rebuild glue for", baseDir
            sys.stdout.flush()
            lock.release()
            os.chdir("glue" + baseDir)
            if baseDir in simulinkSubsystems.keys():
                # Learn about Simulink TASTE-Directives, because the glue compilation may depend on them
                curDir = os.path.abspath(os.getcwd())
                os.chdir("../../" + baseDir + os.sep + baseDir)
                CheckDirectives(baseDir)
                os.chdir(curDir)
                mysystem("\"$GNATGCC\" -c %s -I ../../auto-src %s %s %s %s %s %s %s %s *.c" % (
                    cflagsSoFar + CalculateCFLAGS(baseDir, withPOHIC=False) + CalculateUserCodeOnlyCFLAGS(baseDir),
                    bUseSimulinkMakefiles[baseDir][2],
                    scadeIncludes, simulinkIncludes, cIncludes, guiIncludes, adaIncludes, cyclicIncludes, rtdsIncludes))
            else:
                mysystem("\"$GNATGCC\" -c %s -I ../../auto-src %s %s %s %s %s %s %s %s *.c" % (
                    cflagsSoFar + CalculateCFLAGS(baseDir, withPOHIC=False) + CalculateUserCodeOnlyCFLAGS(baseDir),
                    scadeIncludes, simulinkIncludes, cIncludes, guiIncludes, adaIncludes, cyclicIncludes, rtdsIncludes,
                    vhdlIncludes))
            os.chdir("..")

    lock = multiprocessing.Lock()
    listOfAadl2GluecProcesses = []

    # Disable g_bRetry for this part, it runs under multicore
    global g_bRetry
    retry = g_bRetry
    g_bRetry = False
    runningInstances = 0
    totalCPUs = DetermineNumberOfCPUs()
    allSuccessful = True
    for baseDir in scadeSubsystems.keys() + simulinkSubsystems.keys() + cSubsystems.keys() + cppSubsystems.keys() + adaSubsystems.keys() + ogSubsystems.keys() + rtdsSubsystems.keys() + guiSubsystems + cyclicSubsystems + vhdlSubsystems.keys():
        if runningInstances >= totalCPUs:
            allAreStillAlive = True
            while allAreStillAlive:
                for idx, p in enumerate(listOfAadl2GluecProcesses):
                    childIsAlive = p.is_alive()
                    allAreStillAlive = allAreStillAlive and childIsAlive
                    if not childIsAlive:
                        allSuccessful = allSuccessful and (listOfAadl2GluecProcesses[idx].exitcode==0)
                        del listOfAadl2GluecProcesses[idx]
                        break
                else:
                    time.sleep(1)
            runningInstances -= 1
        p = multiprocessing.Process(target=InvokeAadl2GlueCandCompile, args=(copy.copy(baseDir), lock))
        listOfAadl2GluecProcesses.append(p)
        p.start()
        runningInstances += 1
    for p in listOfAadl2GluecProcesses:
        p.join()
        if p.exitcode != 0:
            allSuccessful = False
    g_bRetry = retry
    if not allSuccessful:
        panic("aadl2glueC invocation failed...")
    os.chdir("..")


def InvokeOcarina(i_aadlFile, depl_aadlFile, md5s, md5hashesFilename, wrappers):
    '''Invokes the Ocarina code generation tool'''
    g_stageLog.info("Invoking Ocarina")
    mkdirIfMissing("GlueAndBuild")
    os.chdir('GlueAndBuild')
#    mysystem("unzip -o \""+vmZip+"\"")
#    if not os.path.isdir("src"):
#       panic("VM Zip file '%s' did not include a src/ subdirectory" % vmZip)
#    mysystem("find src/ -type f -exec mv '{}' . \;")
#    mysystem("rm -rf src")
#    mysystem("\"$GNATGCC\" -c -g *adb")
#    mysystem("\"$GNATGCC\" -c -g n1*ads")
#    mysystem("\"$GNATGCC\" -c -g system_time.ads")
    shutil.copy("../D_view_aadlv2.aadl", "./D_view.aadl")
    # NEW NATIVE GUI allows direct support for AADLv2
    # shutil.copy(depl_aadlFile_aadlv2, ".")
    shutil.copy(depl_aadlFile, ".")
    for x in wrappers:
        mysystem("cp -u \"" + x + "\" . 2>/dev/null || exit 0")
    # Ocarana_config::Root_System_name => "rootsystemname";
    #
    # 2013/09/05: Maxime says this is not necessary, it is hardcoded
    # rootSystemName = getSingleLineFromCmdOutput("tail -1 ../ConcurrencyView/process.aadl")
    # rootSystemName = rootSystemName.strip()
    # rootSystemName = rootSystemName[3:]  # lose the '-' '-' 'space'

    rootSystemName = 'deploymentview.final'
    mainaadl = open('main.aadl.new', 'w')
    for i in os.listdir("../ConcurrencyView/"):
        shutil.copy("../ConcurrencyView/" + i, ".")

    shutil.copy(i_aadlFile, ".")
    installPath = getSingleLineFromCmdOutput("taste-config --prefix")
    ellidissPrefix = '{inst}/share/config_ellidiss/'.format(inst=installPath)
    for i in ('TASTE_IV_Properties.aadl', 'TASTE_DV_Properties.aadl'):
        shutil.copy(ellidissPrefix + i, '.')
    mainaadl.write('''
--  FILE GENERATED AUTOMATICALLY, DO NOT EDIT
--  This file is used internally by the Ocarina code generator
--  to list all AADL files used, the name of the generator and AADL
--  property files of relevance.
system ASSERT_System
properties
 Ocarina_Config::AADL_Files => (%s "%s", %s, "D_view.aadl", "ocarina_components.aadl", "TASTE_IV_Properties.aadl", "TASTE_DV_Properties.aadl");
 Ocarina_Config::Generator => %s;
 Ocarina_Config::Generator_Options => ();
 Ocarina_Config::AADL_Version => AADLv%s;
 Ocarina_Config::Needed_Property_Sets => (
  %s
  "data_model",
  "base_types",
  %s
  value (Ocarina_Config::Deployment),
  value (Ocarina_Config::Cheddar_Properties),
  value (Ocarina_Config::%s_Properties));
  Ocarina_Config::Root_System_name => "%s";
end ASSERT_System;

system implementation ASSERT_System.Impl
end ASSERT_System.Impl;
''' %
                   # NEW NATIVE GUI allows direct support for AADLv2
                   # (  os.path.basename(depl_aadlFile_aadlv2),
                   (
                       '"' + os.path.basename(i_aadlFile) + '",',
                       os.path.basename(depl_aadlFile),
                       ", ".join(["\"" + x + "\"" for x in os.listdir("../ConcurrencyView/") if x != "nodes"]),
                       g_bPolyORB_HI_C and "polyorb_hi_c" or "polyorb_hi_ada",
                       "2",
                       " ",
                       "value (Ocarina_Config::arinc653_properties),",
                       "TASTE",
                       rootSystemName))
    mainaadl.close()
    if not os.path.exists('main.aadl') or md5hash('main.aadl.new')!=md5hash('main.aadl'):
        mysystem("mv main.aadl.new main.aadl")

    # Check to see if the md5 signatures of the source AADL files are the same - if they are, don't invoke ocarina!
    # NEW NATIVE GUI allows direct support for AADLv2
    # aadlSources = [depl_aadlFile_aadlv2, os.path.abspath('D_view.aadl')]
    aadlSources = [depl_aadlFile, os.path.abspath('D_view.aadl')]
#    if tmpAsn1marshallers!="": aadlSources.append( os.path.abspath('asn1_marshallers.aadl') )
    aadlSources.extend([os.path.abspath("../ConcurrencyView/" + x) for x in os.listdir("../ConcurrencyView/") if x != "nodes"])
    invokeOcarina = False
    for i in aadlSources:
        if i not in md5s.keys() or md5s[i]!=md5hash(i):
            invokeOcarina = True
            print "Rebuilding because of", i
            sys.stdout.flush()
            break

    mysystem("cp $(ocarina-config --resources)/AADLv2/ocarina_components.aadl .")
    mysystem('cleanupDV.pl "%s" > a_temp_name && mv a_temp_name "%s"' % (os.path.basename(depl_aadlFile), os.path.basename(depl_aadlFile)))
    if invokeOcarina:
        # banner("Invoking ocarina")
        mysystem("find . -type d \( -iname 'glue*' -prune -o -exec rm -rf '{}' ';' \) 2>/dev/null || exit 0")
        mysystem("ocarina -x main.aadl")
        md = open(g_absOutputDir + os.sep + md5hashesFilename, 'a')
        for i in aadlSources:
            md.write("%s:%s\n" % (i, md5hash(i)))
        md.close()
    else:
        print "No need to reinvoke ocarina"
        sys.stdout.flush()
    os.chdir("../")


def DetectPythonSubsystems():
    '''Detects the Python stubs that will be built'''
    g_stageLog.info("Detecting Python subsystems")
    pythonSubsystems = []
    for line in os.popen("find . -type d -name python", "r").readlines():
        line = line.strip()
        pythonSubsystems.append(line)
    return pythonSubsystems


def CreateIncludePaths(
        scadeSubsystems, simulinkSubsystems, qgencSubsystems, cSubsystems, cppSubsystems, qgenadaSubsystems,
        adaSubsystems, rtdsSubsystems, guiSubsystems, cyclicSubsystems, AdaIncludePath):
    '''Creates the include flags (-I ...) for all subsystems'''
    g_stageLog.info("Creating include paths directive")

    QGenAdaInUse = False
    # qgenadaIncludes = ""
    adaIncludes = ""
    for baseDir in qgenadaSubsystems.keys():
        QGenAdaInUse = True
        break

    scadeIncludes = ""
    for baseDir in scadeSubsystems.keys():
        if os.path.isdir(baseDir + os.sep + baseDir):
            scadeIncludes += ' -I "../../' + baseDir + os.sep + baseDir + os.sep + '"'

    simulinkIncludes = ""
    for baseDir in simulinkSubsystems.keys():
        if os.path.isdir(baseDir + os.sep + baseDir):
            simulinkIncludes += ' -I "../../' + baseDir + os.sep + baseDir + os.sep + '"'

    cIncludes = ""
    cur_dir = os.path.abspath(os.getcwd())
    for baseDir in cSubsystems.keys() + cppSubsystems.keys():
        if os.path.isdir(baseDir + os.sep + baseDir):
            cIncludes += ' -I "../../' + baseDir + os.sep + baseDir + os.sep + '"'
            if QGenAdaInUse:
                AdaIncludePath += ':' + cur_dir + os.sep + baseDir + os.sep + baseDir
    for baseDir in qgencSubsystems.keys():
        if os.path.isdir(baseDir):
            cIncludes += ' -I "../../../' + baseDir + os.sep + '"'

    rtdsIncludes = ""
    for baseDir in rtdsSubsystems.keys():
        if os.path.isdir(baseDir + os.sep + baseDir):
            rtdsIncludes += ' -I "../../' + baseDir + os.sep + baseDir + os.sep + '"'
            rtdsIncludes += ' -I "../../' + baseDir + os.sep + "profile" + os.sep + '"'
            rtdsIncludes += ' -DRTDS_NO_SCHEDULER '

    guiIncludes = ""
    for baseDir in guiSubsystems:
        if os.path.isdir(baseDir + os.sep + baseDir):
            guiIncludes += ' -I "../../' + baseDir + os.sep + baseDir + os.sep + '"'

    adaIncludes = ""
    for baseDir in adaSubsystems.keys():
        if os.path.isdir(baseDir + os.sep + baseDir):
            adaIncludes += ' -I "../../' + baseDir + os.sep + baseDir + os.sep + '"'

    cyclicIncludes = ""
    for baseDir in cyclicSubsystems:
        if os.path.isdir(baseDir + os.sep + baseDir):
            cyclicIncludes += ' -I "../../' + baseDir + os.sep + '"'
    return (scadeIncludes, simulinkIncludes, cIncludes,
            rtdsIncludes, guiIncludes, adaIncludes, cyclicIncludes, AdaIncludePath)


def CheckIfInterfaceViewNeedsUpgrading(i_aadlFile):
    g_stageLog.info("Checking If InterfaceView Needs Upgrading")
    for line in open(i_aadlFile, 'r').readlines():
        if 'taste-directives.aadl' in line.lower():
            panic('\n\nYour interface view needs to be upgraded:\n'+
                  'Please upgrade it with:\n\n'
                  '\ttaste-upgrade-IF-view oldIFview newIFview\n\n'+
                  '...and use the newIFview instead.')


def ApplyPatchForDeploymentViewNeededByOcarinaForNewEllidissTools(depl_aadlFile):
    if not any('Taste::version' in x for x in open(depl_aadlFile).readlines()):
        mysystem('TASTE-DV --convert-deployment-view "%s"' % depl_aadlFile)


def main():
    FixEnvVars()
    cmdLineInformation = ParseCommandLineArgs()
    outputDir, i_aadlFile, depl_aadlFile, \
        bDebug, bProfiling, bUseEmptyInitializers, bCoverage, bKeepCase, cvAttributesFile, \
        stackOptions, AdaIncludePath, AdaDirectories, CDirectories, ExtraLibraries, \
        scadeSubsystems, simulinkSubsystems, qgencSubsystems, qgenadaSubsystems, cSubsystems, \
        cppSubsystems, adaSubsystems, rtdsSubsystems, ogSubsystems, vhdlSubsystems, \
        timerResolution = cmdLineInformation

    os.putenv("WORKDIR", os.path.abspath(outputDir))

    i_aadlFile = os.path.abspath(i_aadlFile)  # use absolute paths to the two views
    depl_aadlFile = os.path.abspath(depl_aadlFile)
    if cvAttributesFile != "":
        cvAttributesFile = os.path.abspath(cvAttributesFile)

    # Not operational yet, the converter hangs...
    # ApplyPatchForDeploymentViewNeededByOcarinaForNewEllidissTools(depl_aadlFile)

    CheckIfInterfaceViewNeedsUpgrading(i_aadlFile)

    # Maintain fully formed ASN.1 grammar in tmp folder
    asn1Grammar = acnFile = cflagsSoFar = ""
    tmpDirName = "/tmp/uniq" + i_aadlFile.replace('/', '')
    os.system("mkdir -p %s ; rm -f %s/*" % (tmpDirName, tmpDirName))
    asn1Grammar = tmpDirName + "/dataview-uniq.asn"
    acnFile = tmpDirName + "/dataview-uniq.acn"
    asn1Grammar = os.path.abspath(asn1Grammar)
    acnFile = os.path.abspath(acnFile)
    baseASN = os.path.basename(asn1Grammar)
    baseACN = os.path.basename(acnFile)

    # Enter build directory
    os.chdir(outputDir)

    # Read any pre-existing MD5 signatures
    md5s, md5hashesFilename = ReadMD5sums(bDebug)

    # Create the AADL DataViews from the ASN.1 grammars referenced in the IF view
    acnFile, isNewGrammar = CreateDataViews(i_aadlFile, asn1Grammar, acnFile, baseASN, md5s, md5hashesFilename)

    # Update global compilation flags (non-partition-specific)
    if bUseEmptyInitializers:
        cflagsSoFar += "-I . -DEMPTY_LOCAL_INIT -DSTATIC=\"\" "
    else:
        cflagsSoFar += "-I . -DSTATIC=\"\" "
    # coverage analysis:
    if bCoverage:
        cflagsSoFar += "-fprofile-arcs -ftest-coverage -DCOVERAGE "
    # profiling:
    if bProfiling:
        cflagsSoFar += " -pg "
    # debug/optimation options (-O/-g) as well as .ELF section consolidation
    if bDebug:
        cflagsSoFar += "-g"

    InvokeASN1Compiler(asn1Grammar, baseASN, acnFile, baseACN, isNewGrammar, bCoverage)

    UnzipSCADEcode(scadeSubsystems)
    majorSimulinkVersion, bUseSimulinkMakefiles = UnzipSimulinkCode(simulinkSubsystems)
    UnzipCcode(cSubsystems, 'C')
    UnzipCcode(cppSubsystems, 'C++')
    AdaIncludePath = UnzipAdaCode(adaSubsystems, AdaIncludePath)
    uniqueSetOfAdaPackages = DetectAdaPackages(adaSubsystems, asn1Grammar)
    UnzipRTDS(rtdsSubsystems)

    InvokeBuildSupport(i_aadlFile, depl_aadlFile, bKeepCase, bDebug, stackOptions, cvAttributesFile, timerResolution)

    wrappers = FindWrappers()
    InvokeOcarina(i_aadlFile, depl_aadlFile, md5s, md5hashesFilename, wrappers)

    AdaIncludePath = AdaSpecialHandling(AdaIncludePath, adaSubsystems)
    ParsePartitionInformation()
    guiSubsystems, AdaIncludePath = DetectGUIsubSystems(AdaIncludePath)
    cyclicSubsystems = DetectCyclicSubsystems()

    InvokeObjectGeodeGenerator(ogSubsystems)

    scadeIncludes, simulinkIncludes, cIncludes, rtdsIncludes, guiIncludes, adaIncludes, cyclicIncludes, AdaIncludePath = \
        CreateIncludePaths(
            scadeSubsystems, simulinkSubsystems, qgencSubsystems, cSubsystems, cppSubsystems, qgenadaSubsystems,
            adaSubsystems, rtdsSubsystems, guiSubsystems, cyclicSubsystems, AdaIncludePath)

    CreateAndCompileGlue(
        asn1Grammar,
        cflagsSoFar,
        scadeIncludes, simulinkIncludes, cIncludes, adaIncludes, rtdsIncludes, guiIncludes, cyclicIncludes,
        scadeSubsystems, simulinkSubsystems, cSubsystems, cppSubsystems, adaSubsystems, rtdsSubsystems, ogSubsystems, guiSubsystems, cyclicSubsystems, vhdlSubsystems,
        md5s, md5hashesFilename,
        majorSimulinkVersion, bUseSimulinkMakefiles)

    pythonSubsystems = DetectPythonSubsystems()

    BuildSCADEsystems(scadeSubsystems, CDirectories, cflagsSoFar)
    BuildSimulinkSystems(simulinkSubsystems, CDirectories, cflagsSoFar, bUseSimulinkMakefiles)
    BuildCsystems(cSubsystems, CDirectories, cflagsSoFar)
    BuildCPPsystems(cppSubsystems, CDirectories, cflagsSoFar)
    BuildAdaSystems_C_code(adaSubsystems, CDirectories, uniqueSetOfAdaPackages, cflagsSoFar)
    BuildObjectGeodeSystems(ogSubsystems, CDirectories, cflagsSoFar)
    BuildRTDSsystems(rtdsSubsystems, CDirectories, cflagsSoFar)
    BuildVHDLsystems_C_code(vhdlSubsystems, CDirectories, cflagsSoFar)

    commentedGUIfilters = BuildGUIs(guiSubsystems, cflagsSoFar, asn1Grammar)

    BuildPythonStubs(pythonSubsystems, asn1Grammar, acnFile)

    shutil.rmtree(tmpDirName)

    BuildCyclicSubsystems(cyclicSubsystems, cflagsSoFar)

    RenameCommonlyNamedSymbols(
        scadeSubsystems,
        simulinkSubsystems,
        cSubsystems,
        cppSubsystems,
        adaSubsystems,
        rtdsSubsystems,
        ogSubsystems,
        guiSubsystems,
        cyclicSubsystems,
        vhdlSubsystems)

    AdaIncludePath = InvokeOcarinaMakefiles(
        scadeSubsystems, simulinkSubsystems, cSubsystems, cppSubsystems, adaSubsystems, rtdsSubsystems, ogSubsystems, guiSubsystems, cyclicSubsystems, vhdlSubsystems,
        cflagsSoFar, CDirectories, AdaDirectories, AdaIncludePath, ExtraLibraries,
        bDebug, bUseEmptyInitializers, bCoverage, bProfiling)

    GatherAllExecutableOutput(outputDir, pythonSubsystems, vhdlSubsystems, tmpDirName, commentedGUIfilters, bDebug, i_aadlFile)
    CopyDatabaseFolderIfExisting()

if __name__ == "__main__":
    main()

# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
