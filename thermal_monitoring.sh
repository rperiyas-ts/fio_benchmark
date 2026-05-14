while true
do
  date | tee -a new_enclosure_test_temps.txt
  echo "--------------------------------------------------" | tee -a new_enclosure_test_temps.txt
  for dev in $(sudo nvme list | awk '/^\/dev/ {print $1}')
    do
      sn=$(sudo nvme id-ctrl $dev | awk '/^sn / {print $3}')
      temp=$(sudo nvme smart-log $dev | awk -F':' '/^temperature/ {print $2}' | xargs)
      echo "$dev | SN: $sn | Temp: $temp"  | tee -a new_enclosure_test_temps.txt
    done
  sleep 15
done