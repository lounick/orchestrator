This utility is responsible for all the steps of building TASTE binaries.

Starting from...

    (a) the Interface/Deployment/Data views,
    (b) the user code of the subsystems,

it will:

    Extract the ASN.1 DataViews
    Invoke the ASN1 Compiler
    Unpack any input user code (SCADE, Simulink, RTDS, C, C++, Ada)
    Invoke the ESA BuildSupport tool
    Identify the generated Wrappers
    Invoke Ocarina to generate the containers from the Vertical xform
    Perform special handling for Ada subsystems
    Identify the deployment Partition information
    Detect any GUI subSystems that must be automatically created
    Create the include paths for compiling C code
    Create the run-time type converters (ASN.1 <-> SCADE/Simulink/etc)
    Detect whether Python bridges must be automatically created
    Compile the user provided code for
        SCADE systems
        OpenGEODE systems
        Simulink systems
        C and C++ systems
        Ada Systems
        RTDS systems
    Compile the automatically generated drivers for FPGA designs
    Build the automatically generated GUIs
    Build the automatically generaetd Python bridges
    Build the cyclic subsystems
    Identify and rename any conflicting common symbol in the object files
    Invoke the Ocarina generated Makefiles (i.e. build and link it all)
    Gather all executable output into output/binaries folder
