#!/bin/bash

#Activate modules
module load cuda12.3/toolkit

# Prepare a local script
local_launch="$(pwd)/launch.sh.local"
# Copy/paste the file
cp "$(pwd)/launch.sh" "$local_launch"
# Use sed to replace the string '%CURRENT_FOLDER%' with the target path
sed -i "s+%CURRENT_FOLDER%+$(pwd)+g" "$local_launch"

# Run the bash job
JOBID=$(sbatch --parsable "$local_launch")

# Wait until it is spawned
echo "[runit.sh] Spawned job $JOBID; waiting until scheduled..."
while [[ -z "$(squeue | grep $JOBID | grep R)" ]]; do sleep 1; done
# Wait one more sec for good measure, to ensure the log exists
sleep 1

# Attach to the log
log_path="$(pwd)/logs_${JOBID}.out"
echo "[runit.sh] Attaching to '$log_path'; if detached, you can re-attach using 'tail -f \"$log_path\"'"
tail -f "$log_path"

