// parameter defaults
params.output = 'results'

env {
    NUMBA_CACHE_DIR = './temp/numba_cache'
    MPLCONFIGDIR = './temp/matplotlib'
    XDG_CACHE_HOME = './temp/xdg_home'
}

apptainer.enabled = true

// process {
//     scratch = true              // use worker node's local hard drive
//     executor = 'slurm'
//     clusterOptions = '--nodes=1 --ntasks=1 --account=${params.account}'

//     // resource defaults
//     cpu = 1
//     memory = '4 GB'
//     time = '2h'
    
//     queueSize = 100
//     submitRateLimit = '1/1sec'  // this is aggressive, need to lower for production
//     pollInterval = '5 sec'      // same^

//     // -----------------------------------------
//     // notes
//     // executor = 'hq'          // todo: consider https://github.com/It4innovations/hyperqueue
//     // stageInMode = 'copy'     // some intermediates are large reference databases and should not be copied
// }
