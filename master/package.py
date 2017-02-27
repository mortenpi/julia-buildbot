## A note on unneecssary complexity
# We have gone through a few different standards on naming Julia's build artifacts.
# The latest, as of this writing, is the `sf/consistent_distnames` branch on github,
# and simplifies things relative to earlier versions.  However, this buildbot needs
# to be able to build/upload Julia versions of all reasonably recent versions.
# `sf/consistent_distnames` should be merged before the 0.6 release, which means
# that once the release _after_ 0.6 is out in the wild and 0.5 is put to rest,
# we can safely remove anything that talks about non-`sf/consistent_distnames`
# compatibility/workarounds.

def should_build_branch(branch):
    if branch in ["master", "sf/buildbot_testing"]:
        return True
    if branch.startswith("release-"):
        return True
    return False

# Helper function to generate the necessary julia invocation to get metadata
# about this build such as major/minor versions
@util.renderer
def make_julia_version_command(props_obj):
    command = [
        "usr/bin/julia",
        "-e",
        "println(\"$(VERSION.major).$(VERSION.minor).$(VERSION.patch)\\n$(Base.GIT_VERSION_INFO.commit[1:10])\")"
    ]

    if is_windows(props_obj):
        command[0] += '.exe'
    return command

# Parse out the full julia version generated by make_julia_version_command's command
def parse_julia_version(return_code, stdout, stderr):
    lines = stdout.split('\n')
    return {
        "majmin": lines[0][:lines[0].rfind('.')],
        "version": lines[0].strip(),
        "shortcommit": lines[1].strip(),
    }

def parse_git_log(return_code, stdout, stderr):
    lines = stdout.split('\n')
    return {
        "commitmessage": lines[0],
        "commitname": lines[1],
        "commitemail": lines[2],
        "authorname": lines[3],
        "authoremail": lines[4],
    }

def gen_local_filename(props_obj):
    props = props_obj_to_dict(props_obj)

    # Get the output of the `make print-JULIA_BINARYDIST_FILENAME` step
    artifact = "{artifact_filename}".format(**props).strip()

    # First, see if we got a JULIA_BINARYDIST_FILENAME output
    if artifact[:26] == "JULIA_BINARYDIST_FILENAME=" and len(artifact) > 26:
        return artifact[26:] + "{os_pkg_ext}".format(**props)
    else:
        # If not, use non-sf/consistent_distnames naming
        if is_mac(props_obj):
            return "contrib/mac/app/Julia-{version}-{shortcommit}.{os_pkg_ext}".format(**props)
        elif is_windows(props_obj):
            return "julia-{version}-{tar_arch}.{os_pkg_ext}".format(**props)
        else:
            # We made bad decisions in the past
            if tar_arch == "armv7l":
                return "julia-{shortcommit}-Linux-arm.{os_pkg_ext}".format(**props)
            return "julia-{shortcommit}-Linux-{tar_arch}.{os_pkg_ext}".format(**props)


def gen_upload_filename(props_obj):
    props = props_obj_to_dict(props_obj)
    return "julia-{shortcommit}-{os_name}{bits}.{os_pkg_ext}".format(**props)


def gen_upload_path(props_obj):
    up_arch = props_obj.getProperty("up_arch")
    majmin = props_obj.getProperty("majmin")
    upload_fname = props_obj.getProperty("upload_filename")
    os = get_os_name(props_obj)
    return "julianightlies/test/bin/%s/%s/%s/%s"%(os, up_arch, majmin, upload_fname)

def gen_latest_upload_path(props_obj):
    up_arch = props_obj.getProperty("up_arch")
    upload_filename = props_obj.getProperty("upload_filename")
    if upload_filename[:6] == "julia-":
        upload_filename = "julia-latest-%s"%(upload_filename[6:])
    os = get_os_name(props_obj)
    return "julianightlies/test/bin/%s/%s/%s"%(os, up_arch, upload_filename)


def gen_download_url(props_obj):
    base = 'https://s3.amazonaws.com'
    return '%s/%s'%(base, gen_upload_path(props_obj))



# This is a weird buildbot hack where we really want to parse the output of our
# make command, but we also need access to our properties, which we can't get
# from within an `extract_fn`.  So we save the output from a previous
# SetPropertyFromCommand invocation, then invoke a new command through this
# @util.renderer nonsense.  This function is supposed to return a new command
# to be executed, but it has full access to all our properties, so we do all our
# artifact filename parsing/munging here, then return ["true"] as the step
# to be executed.
@util.renderer
def munge_artifact_filename(props_obj):
    # Generate our local and upload filenames
    local_filename = gen_local_filename(props_obj)
    upload_filename = gen_upload_filename(props_obj)

    props_obj.setProperty("local_filename", local_filename, "munge_artifact_filename")
    props_obj.setProperty("upload_filename", upload_filename, "munge_artifact_filename")
    return ["true"]

@util.renderer
def render_upload_command(props_obj):
    upload_path = gen_upload_path(props_obj)
    upload_filename = props_obj.getProperty("upload_filename")
    return ["/bin/bash", "-c", "~/bin/try_thrice ~/bin/aws put --fail --public %s /tmp/julia_package/%s"%(upload_path, upload_filename)]

@util.renderer
def render_latest_upload_command(props_obj):
    latest_upload_path = gen_latest_upload_path(props_obj)
    upload_filename = props_obj.getProperty("upload_filename")
    return ["/bin/bash", "-c", "~/bin/try_thrice ~/bin/aws put --fail --public %s /tmp/julia_package/%s"%(latest_upload_path, upload_filename)]

@util.renderer
def render_download_url(props_obj):
    return gen_download_url(props_obj)

@util.renderer
def render_make_app(props_obj):
    props = props_obj_to_dict(props_obj)

    new_way = "make {flags} app".format(**props)
    old_way = "make {flags} -C contrib/mac/app && mv contrib/mac/app/*.dmg {local_filename}".format(**props)

    # We emit a bash command that attempts to run `make app` (which is the nice
    # `sf/consistent_distnames` shortcut), and if that fails, it runs the steps
    # manually, which boil down to `make -C contrib/mac/app` and moving the
    # result to the top-level, where we can find it.
    return [
        "/bin/bash",
        "-c",
        "~/unlock_keychain.sh && (%s || (%s))"%(new_way, old_way)
    ]

julia_package_env = {
    'CFLAGS':None,
    'CPPFLAGS': None,
    'LLVM_CMAKE': util.Property('llvm_cmake', default=None),
    'JULIA_CPU_CORES': '6',
}

# Steps to build a `make binary-dist` tarball that should work on just about every linux ever
julia_package_factory = util.BuildFactory()
julia_package_factory.useProgress = True
julia_package_factory.addSteps([
    # Fetch first (allowing failure if no existing clone is present)
    steps.ShellCommand(
        name="git fetch",
        command=["git", "fetch"],
        flunkOnFailure=False
    ),

    # Clone julia
    steps.Git(
        name="Julia checkout",
        repourl=util.Property('repository', default='git://github.com/JuliaLang/julia.git'),
        mode='incremental',
        method='clean',
        submodules=True,
        clobberOnFailure=True,
        progress=True
    ),

    # Ensure gcc and cmake are installed on OSX
    steps.ShellCommand(
        name="Install necessary brew dependencies",
        command=["brew", "install", "gcc", "cmake"],
        doStepIf=is_mac,
        flunkOnFailure=False
    ),

    # make clean first
    steps.ShellCommand(
        name="make cleanall",
        command=["/bin/bash", "-c", util.Interpolate("make %(prop:flags)s cleanall")],
        env=julia_package_env,
    ),

    # Make, forcing some degree of parallelism to cut down compile times
    # Also build `debug` and `release` in parallel, we should have enough RAM for that now
    steps.ShellCommand(
        name="make",
        command=["/bin/bash", "-c", util.Interpolate("make -j3 %(prop:flags)s debug release")],
        haltOnFailure = True,
        timeout=3600,
        env=julia_package_env,
    ),

    # Test this build
    steps.ShellCommand(
        name="make testall",
        command=["/bin/bash", "-c", util.Interpolate("make %(prop:flags)s testall")],
        haltOnFailure = True,
        timeout=3600,
        env=julia_package_env,
    ),

    # Make win-extras on windows
    steps.ShellCommand(
        name="make win-extras",
        command=["/bin/bash", "-c", util.Interpolate("make %(prop:flags)s win-extras")],
        haltOnFailure = True,
        doStepIf=is_windows,
        env=julia_package_env,
    ),

    # Set a bunch of properties that are useful down the line
    steps.SetPropertyFromCommand(
        name="Get commitmessage",
        command=["git", "log", "-1", "--pretty=format:%s%n%cN%n%cE%n%aN%n%aE"],
        extract_fn=parse_git_log,
        want_stderr=False
    ),
    steps.SetPropertyFromCommand(
        name="Get julia version/shortcommit",
        command=make_julia_version_command,
        extract_fn=parse_julia_version,
        want_stderr=False
    ),
    steps.SetPropertyFromCommand(
        name="Get build artifact filename",
        command=["make", "print-JULIA_BINARYDIST_FILENAME"],
        property="artifact_filename",
    ),
    steps.SetPropertyFromCommand(
        name="Munge artifact filename",
        command=munge_artifact_filename,
        property="dummy",
    ),

    # Make binary-dist to package it up
    steps.ShellCommand(
        name="make binary-dist",
        command=["/bin/bash", "-c", util.Interpolate("make %(prop:flags)s binary-dist")],
        haltOnFailure = True,
        timeout=3600,
        env=julia_package_env,
    ),

    # On OSX, deal with non-sf/consistent_distnames makefile nonsense by wrapping up all
    # the complexity into `render_make_app`.
    steps.ShellCommand(
        name="make .app",
        command=render_make_app,
        haltOnFailure = True,
        doStepIf=is_mac,
        env=julia_package_env,
    ),

    # Transfer the result to the buildmaster for uploading to AWS
    steps.MasterShellCommand(
        name="mkdir julia_package",
        command=["mkdir", "-p", "/tmp/julia_package"]
    ),

    steps.FileUpload(
        workersrc=util.Interpolate("%(prop:local_filename)s"),
        masterdest=util.Interpolate("/tmp/julia_package/%(prop:upload_filename)s")
    ),

    # Upload it to AWS and cleanup the master!
    steps.MasterShellCommand(
        name="Upload to AWS",
        command=render_upload_command,
        doStepIf=should_upload,
        haltOnFailure=True
    ),
    steps.MasterShellCommand(
        name="Upload to AWS (latest)",
        command=render_latest_upload_command,
        doStepIf=should_upload_latest,
        haltOnFailure=True
    ),

    steps.MasterShellCommand(
        name="Cleanup Master",
        command=["rm", "-f", util.Interpolate("/tmp/julia_package/%(prop:upload_filename)s")],
        doStepIf=should_upload
    ),

    # Trigger a download of this file onto another worker for coverage purposes
    steps.Trigger(schedulerNames=["Julia Coverage Testing"],
        set_properties={
            'url': render_download_url,
            'commitmessage': util.Property('commitmessage'),
            'commitname': util.Property('commitname'),
            'commitemail': util.Property('commitemail'),
            'authorname': util.Property('authorname'),
            'authoremail': util.Property('authoremail'),
            'shortcommit': util.Property('shortcommit'),
        },
        waitForFinish=False,
        doStepIf=should_run_coverage
    )
])

# Build a builder-worker mapping based off of the parent mapping in inventory.py
packager_mapping = {("package_" + k): v for k, v in builder_mapping.iteritems()}

# Add a few builders that don't exist in the typical mapping
packager_mapping["build_ubuntu32"] = "ubuntu16_04-x86"
packager_mapping["build_ubuntu64"] = "ubuntu16_04-x64"
packager_mapping["build_centos64"] = "centos7_3-x64"


packager_scheduler = schedulers.AnyBranchScheduler(
    name="Julia binary packaging",
    change_filter=util.ChangeFilter(
        project=['JuliaLang/julia','staticfloat/julia'],
        branch_fn=should_build_branch
    ),
    builderNames=packager_mapping.keys(),
    treeStableTimer=1
)
c['schedulers'].append(packager_scheduler)

for packager, worker in packager_mapping.iteritems():
    c['builders'].append(util.BuilderConfig(
        name=packager,
        workernames=[worker],
        tags=["Packaging"],
        factory=julia_package_factory
    ))


# Add a scheduler for building release candidates/triggering builds manually
force_build_scheduler = schedulers.ForceScheduler(
    name="force_julia_package",
    label="Force Julia build/packaging",
    builderNames=packager_mapping.keys(),
    reason=util.FixedParameter(name="reason", default=""),
    codebases=[
        util.CodebaseParameter(
            "",
            name="",
            branch=util.FixedParameter(name="branch", default=""),
            repository=util.FixedParameter(name="repository", default=""),
            project=util.FixedParameter(name="project", default="Packaging"),
        )
    ],
    properties=[]
)
c['schedulers'].append(force_build_scheduler)
