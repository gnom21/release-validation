#!/usr/bin/env python
from __future__ import print_function
import jinja2
import json
import classad
import re, os, errno
from shutil import copy2, rmtree
from argparse import ArgumentParser, REMAINDER
from lxml import etree
import subprocess

prog_path = os.path.abspath(os.path.dirname(__file__))

ap = ArgumentParser()
ap.add_argument("--dryrun", "-n", dest="dry_run", default=False, action="store_true",
                help="Generate Makeflow script that does not actually run any job")
ap.add_argument("--workdir", "-w", dest="work_dir", default="work",
                help="Makeflow work directory (defaults to \"work\")")
ap.add_argument("--parse-jdl", "-p", dest="parse_jdl", default=False, action="store_true",
                help="Only parse the JDL, show the result and exit")
ap.add_argument("--summary", "-s", dest="summary", default=False, action="store_true",
                help="Only show summary without creating any file and exit")
ap.add_argument("--start-at", "-t", dest="start_at", default="sim",
                choices=["sim", "merge", "qaplots"],
                help="Start at the defined stage, assuming the previous stages were successful")
ap.add_argument("--force", dest="force", default=False, action="store_true",
                help="Force removal of working directory (you will lose files!)")
ap.add_argument("--run", "-r", dest="run", default=False, action="store_true",
                help="Automatically run workflow after generating the manifest")
ap.add_argument("jdl",
                help="AliEn JDL steering the jobs")
ap.add_argument("makeflow_opts", nargs=REMAINDER,
                help="Options for the makeflow command")
args = ap.parse_args()

j2env = jinja2.Environment()
j2env.filters["basename"] = lambda x: os.path.basename(x)

def preprocess_var(s):
  if isinstance(s, list):
    s = ",".join(s)
  s = s.replace("#alien_counter#", "#alien_counter_i#")
  return re.sub("#alien_counter_([^#]+)#", "%\\1", s)

def gen_runjob(output_file, jdl, dry_run):
  runjob = j2env.from_string("""#!/bin/bash -ex
type zip
JOBHASH=$(echo "$*" | sha1sum | awk '{print $1}')
JOBID=$1
DONEFILE="$PWD/$2"
shift 2
rm -rf job-$JOBHASH
mkdir job-$JOBHASH
cd job-$JOBHASH
INPUT_LIST=({% for x in input_list -%}
             "{{x}}"
             {% endfor %})
OUTPUT_LIST=({% for x in output_list -%}
             "{{x}}"
             {% endfor %})
for INP in "${INPUT_LIST[@]}"; do
  cp -v ../"$INP" .
done
{% if source_env -%}
# Set custom environment
source "{{source_env}}"
{% else -%}
eval $(/cvmfs/alice.cern.ch/bin/alienv printenv {{packages|join(",")}})
{% endif -%}
{{""}}
# Job environment
{% for x in environment -%}
export {{x}}="{{environment[x]|replace('"', '\\\\"')}}"
{% endfor -%}
{{""}}
# Output directory
ALIEN_JDL_OUTPUTDIR=$(printf "$ALIEN_JDL_OUTPUTDIR" "$JOBID")
echo "Output will be in $ALIEN_JDL_OUTPUTDIR"

# Execute command and ignore errors
{% if dry_run -%}
echo "Doing nothing: dry run" > stdout.log
{% else -%}
ARGS=$(printf -- "{{args}}" "$JOBID")
PROG="{{executable}}"
type "$PROG" &> /dev/null || PROG="{{executable|basename()}}"
type "$PROG" &> /dev/null || PROG="$ALIDPG_ROOT/bin/{{executable|basename()}}"
type "$PROG" &> /dev/null || PROG="./{{executable|basename()}}"
MAINERR=0
$PROG "$@" $ARGS  > >(tee stdout.log) \\
                 2> >(tee stderr.log) || MAINERR=$?
{% endif -%}
[[ $MAINERR != 0 ]] && echo "Exited with errors ($MAINERR)" || echo "Exited with no errors";

# Compress output according to the output list
mkdir to_transfer
shopt -s extglob
for OUT in "${OUTPUT_LIST[@]}"; do
  ZIP=${OUT%%:*}
  FILES=${OUT#*:}
  [[ $ZIP == $OUT ]] && { echo "Not archiving $OUT"; mv -v $OUT to_transfer/ || true; continue; }
  [[ $FILES =~ \.root(,|$) ]] && ZIP_COMP="-0" || ZIP_COMP="-9"
  FILES=${FILES//,/ }
  echo $ZIP will contain $FILES
  ZIPERR=0
  zip $ZIP_COMP tmparchive.zip $FILES || ZIPERR=$?  # exitcode 12 is fine ==> "nothing to do"
  [[ $ZIPERR == 12 ]] && { echo "Zip $ZIP would be empty: not creating"; continue; } \\
                      || [[ $ZIPERR == 0 ]]
  rm -f $FILES  # same files cannot be in more than one archive
  mv tmparchive.zip to_transfer/$ZIP
done

# Copy files to destination (filesystem or xrootd)
PROTO=${ALIEN_JDL_OUTPUTDIR%%://*}
[[ $PROTO != $ALIEN_JDL_OUTPUTDIR ]] || { PROTO=local; mkdir -p $ALIEN_JDL_OUTPUTDIR; }
[[ $PROTO == local || $PROTO == root ]] || { echo "Output protocol $PROTO not supported;" exit 1; }
pushd to_transfer
  while read FILE; do
    for ((I=1; I<=5; I++)); do
      ERR=0
      echo "Transferring $FILE to $ALIEN_JDL_OUTPUTDIR (attempt $I/5)"
      case $PROTO in
        local) mkdir -p $(dirname "$ALIEN_JDL_OUTPUTDIR/$FILE"); cp -v $FILE $ALIEN_JDL_OUTPUTDIR/$FILE && break || ERR=$? ;;
        root)  xrdcp -f $FILE $ALIEN_JDL_OUTPUTDIR/$FILE && break || ERR=$? ;;
      esac
    done
  done < <(find . -type f | sed -e 's|^\./||')
popd

# Cleanup all
rm -rf *

# Signal success
echo Workflow execution completed: $PROG $ARGS exited with $MAINERR
touch $DONEFILE
""")
  with open(output_file, "w") as mf:
    mf.write(runjob.render(output_list = jdl["Output"],
                           input_list  = jdl["InputFile"],
                           packages    = jdl["Packages"],
                           source_env  = jdl.get("SourceEnvScript", None),
                           environment = jdl["Environment"],
                           dry_run     = dry_run,
                           executable  = jdl["Executable"],
                           args        = jdl["SplitArguments"]))
  os.chmod(output_file, int("755", 8))

def get_alien_xml(pattern, joba, jobb):
  pattern = preprocess_var(pattern)
  root = etree.Element("alien")
  coll = etree.SubElement(root, "collection", name="alien_collection.xml")
  for evt in range(joba,jobb+1):
    evnt = etree.SubElement(coll, "event", name=str(evt))
    tfil = etree.SubElement(evnt, "file", turl=pattern % evt, type="f")
  return etree.tostring(root, pretty_print=True)

def get_preprocessed_jdl(jdl_fn, override={}, append={}, delete=[]):
  jdl = classad.parse(open(jdl_fn).read(), ignore_errors=True)

  # Process overrides
  for k in override:
    jdl[k] = override[k]

  # Command-line arguments, with alien_counter (will be substituted with the job number)
  jdl["SplitArguments"] = preprocess_var(jdl["SplitArguments"])

  # Packages (filter out jemalloc)
  jdl["Packages"] = [ x for x in jdl["Packages"] if not "jemalloc" in x ]

  # Job range ("Split" parameter)
  m = re.search("[^:]:([0-9]+)-([0-9]+)", jdl["Split"])
  jdl["JobRange"] = [ int(m.group(1)), int(m.group(2)) ]

  # Remove @disk spec from output list
  jdl["Output"] = [ o.split("@", 1)[0] for o in jdl["Output"] ]

  # Input list: get the base path only (assume files are in the current directory or a "system" one)
  jdl["InputFile"] = [ os.path.basename(x) for x in jdl.get("InputFile", []) ]
  if jdl["SourceEnvScript"]:
    jdl["InputFile"].append(os.path.basename(jdl["SourceEnvScript"]))

  # Process appends
  for k in append:
    if k in jdl:
      jdl[k] += append[k]

  # Job environment
  environment = {}
  for v in jdl["JDLVariables"]:
    environment["ALIEN_JDL_"+v.upper()] = preprocess_var(jdl.get(v, ""))  # with ALIEN_JDL_
  for v in jdl["ExtraVariables"]:
    environment[v] = preprocess_var(jdl.get(v, ""))  # exported as-is
  jdl["Environment"] = environment

  # Remove unneeded variables (cleanup)
  all_vars = jdl.keys()
  whitelist = [ "SplitArguments", "Executable", "Packages", "JobRange", "JobRange", "Output",
                "InputFile", "Environment", "NextStages", "OutputDir", "SourceEnvScript" ] + \
              override.keys() + append.keys()
  for k in all_vars:
    if k in delete or k not in whitelist:
      del jdl[k]

  return jdl

# First tier of jobs
jdl = get_preprocessed_jdl(args.jdl)

# Second tier of jobs: Space Point Calibration
jdl_spc = {}
if "SpacePointCalibration" in jdl.get("NextStages", []):
  jdl_spc = get_preprocessed_jdl(args.jdl,
              override={ "Output": [ "spcm_archive.zip:pyxsec*.root,AODQA.root,AliAOD*.root,FilterEvents_Trees*.root,*.stat*,EventStat_temp*.root,Residual*.root,TOFcalibTree.root,std*,fileinfo*.log@disk=2" ],
                         "OutputDir": os.path.join(os.path.dirname(jdl["OutputDir"]),
                                                   "SpacePointCalibrationMerge",
                                                   os.path.basename(jdl["OutputDir"])),
                         "Executable": "spc_merge.sh",
                         "SplitArguments": "" },
              append={ "InputFile": [ "spc_merge.sh", "spc_merge.C", "spc.xml" ] },
              delete={ "NextStages", "JobRange" })
  xml_spc = get_alien_xml(os.path.join(jdl["OutputDir"], "FilterEvents_Trees.root"),
                          jdl["JobRange"][0], jdl["JobRange"][1])

# Second tier of jobs: FinalQA
jdl_fqa = {}
if "FinalQA" in jdl.get("NextStages", []):
  jdl_fqa = get_preprocessed_jdl(args.jdl,
              override={ "Output": [ "QA_merge_log_archive.zip:std*,fileinfo*.log@disk=1",
                                     "QA_merge_archive.zip:*QAresults*.root,EventStat_temp*.root,trending*.root,event_stat*.root,*.stat*@disk=3" ],
                         "OutputDir": os.path.join(os.path.dirname(jdl["OutputDir"])),
                         "Executable": "train_merge.sh",
                         "SplitArguments": "finalqa.xml 5" },  # 5 is the stage number
              append={ "InputFile": [ "train_merge.sh", "finalqa.xml" ] },
              delete={ "NextStages", "JobRange" })
  xml_fqa = get_alien_xml(os.path.join(jdl["OutputDir"], "QA_archive.zip"),
                          jdl["JobRange"][0], jdl["JobRange"][1])
  jdl_qap = get_preprocessed_jdl(args.jdl,
              override={ "Output": [ "qa_plots/*", "std*" ],
                         "OutputDir": os.path.join(os.path.dirname(jdl["OutputDir"]), "QAplots"),
                         "Executable": "qa_plots.sh",
                         "SplitArguments": os.path.join(os.path.dirname(jdl["OutputDir"]), "QA_merge_archive.zip") },
              append={ "InputFile": [ "qa_plots.sh" ] },
              delete={ "NextStages", "JobRange" })

if args.parse_jdl:
  print("# First tier of jobs")
  print(json.dumps(jdl, indent=2))
  if jdl_spc:
    print("\n# Space Point Calibration")
    print(json.dumps(jdl_spc, indent=2))
  if jdl_fqa:
    print("\n# Final QA")
    print(json.dumps(jdl_fqa, indent=2))
    print("\n# QA Plots")
    print(json.dumps(jdl_qap, indent=2))
  exit(0)

# Summary
print("""Running the workflow with the following configuration:

Packages:
%(packages)s

%(numjobs)d total jobs, with job IDs from %(joba)d to %(jobb)d (included), will execute the command:
%(command)s

Input files (must be in the current directory, will be made available to each job):
%(input)s

Output files (archives with content listed):
%(output)s

Environment variables available to the jobs:
%(env)s
""" % { "packages" : " * "+"\n * ".join(jdl["Packages"]),
        "numjobs"  : jdl["JobRange"][1]-jdl["JobRange"][0]+1,
        "joba"     : jdl["JobRange"][0],
        "jobb"     : jdl["JobRange"][1],
        "command"  : jdl["Executable"] + " " + jdl["SplitArguments"],
        "input"    : " * "+"\n * ".join(jdl["InputFile"]),
        "output"   : " * "+"\n * ".join([ " ==> ".join(x.split(":", 1)) for x in jdl["Output"] ]),
        "env"      : " * "+"\n * ".join([ "%s ==> %s"%(x,jdl["Environment"][x]) for x in jdl["Environment"]])

})
if args.summary:
  exit(0)

# Create working directory. From now on we write to disk, not before
if args.force:
  try:
    rmtree(args.work_dir)
  except:
    pass
try:
  os.mkdir(args.work_dir)
except OSError as e:
  if e.errno == errno.EEXIST:
    print("Cannot create output directory \"%s\": remove existing one first" % args.work_dir)
  else:
    print("Cannot create output directory \"%s\": %s" % (args.workdir, e))
  exit(1)

# Create XML files and scripts for tiers > first
if jdl_spc:
  with open(os.path.join(args.work_dir, "spc.xml"), "w") as xml: xml.write(xml_spc)
  gen_runjob(os.path.join(args.work_dir, "runspc.sh"), jdl_spc, args.dry_run)
if jdl_fqa:
  with open(os.path.join(args.work_dir, "finalqa.xml"), "w") as xml: xml.write(xml_fqa)
  gen_runjob(os.path.join(args.work_dir, "runfinalqa.sh"), jdl_fqa, args.dry_run)
  gen_runjob(os.path.join(args.work_dir, "runqaplots.sh"), jdl_qap, args.dry_run)

# Copy input files in the work directory
for f in jdl["InputFile"] + jdl_spc.get("InputFile", []) + jdl_fqa.get("InputFile", []) + jdl_qap.get("InputFile", []):
  dest = os.path.join(args.work_dir, f)
  if os.path.isfile(dest): continue
  try:
    copy2(f, dest)  # current dir first
  except:
    try:
      copy2(os.path.join(prog_path, f), dest)  # fallback on this executable's path
    except IOError:
      print("Cannot copy input file \"%s\", please make it available in the current directory"
            " or remove it from the JDL" % f)
      exit(1)

# Produce the wrapper script for the jobs
gen_runjob(os.path.join(args.work_dir, "runjob.sh"), jdl, args.dry_run)

# Produce the Makeflow
makeflow = j2env.from_string("""# Automatically generated

{% for jobindex in range(joba,jobb) -%}
job{{ "%04d"|format(jobindex) }}.done: runjob.sh {{input_list|join(" ")}}
	./runjob.sh {{jobindex}} job{{"%04d"|format(jobindex)}}.done

{% endfor -%}
{% if "SpacePointCalibration" in next_stages -%}
spc.done: runspc.sh {{input_list|join(" ")}}{% for jobindex in range(joba,jobb) %} job{{"%04d"|format(jobindex)}}.done{% endfor %}
	./runspc.sh 0 spc.done
{% endif -%}
{% if "FinalQA" in next_stages -%}
finalqa.done: runfinalqa.sh {{input_list|join(" ")}}{% for jobindex in range(joba,jobb) %} job{{"%04d"|format(jobindex)}}.done{% endfor %}
	./runfinalqa.sh 0 finalqa.done

qaplots.done: runqaplots.sh finalqa.done {{input_list|join(" ")}}
	./runqaplots.sh 0 qaplots.done
{% endif %}
""")
makeflow_fn = os.path.join(args.work_dir, "Makeflow")
with open(makeflow_fn, "w") as mf:
  mf.write(makeflow.render(pars        = jdl["SplitArguments"],
                           input_list  = jdl["InputFile"],
                           joba        = jdl["JobRange"][0],
                           jobb        = jdl["JobRange"][1]+1,
                           next_stages = jdl.get("NextStages", [])))

if not args.run:
  print("""Execute the workflow with:
      cd %(workdir)s
      makeflow %(makeflow_opts)s
  """ % { "workdir": args.work_dir, "makeflow_opts": " ".join(args.makeflow_opts) })

# Touch placeholder files to skip earlier stages
if args.start_at in [ "merge", "qaplots" ]:
  for jobindex in range(jdl["JobRange"][0], jdl["JobRange"][1]+1):
    open(os.path.join(args.work_dir, "job%04d.done"%jobindex), "w").close()
if args.start_at == "qaplots":
  for done in [ "finalqa.done", "spc.done" ]:
    open(os.path.join(args.work_dir, done), "w").close()

# Run Makeflow in dryrun mode to pretend we have completed the earlier stages
if args.start_at != "sim":
  wd = os.getcwd()
  os.chdir(args.work_dir)
  devnull = open(os.devnull)
  subprocess.check_call([ "makeflow", "-T", "dryrun" ], stdout=devnull, stderr=devnull)
  os.chdir(wd)

if args.run:
  os.chdir(args.work_dir)
  try:
    subprocess.check_call([ "time", "makeflow" ] + args.makeflow_opts)
  except subprocess.CalledProcessError as e:
    exit(e.returncode if e.returncode > 0 else 128-e.returncode)
  exit(0)