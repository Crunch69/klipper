# Printer heater support
#
# Copyright (C) 2016-2018  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math, logging, threading


######################################################################
# Heater
######################################################################

KELVIN_TO_CELCIUS = -273.15
MAX_HEAT_TIME = 5.0
AMBIENT_TEMP = 25.
PID_PARAM_BASE = 255.

class error(Exception):
    pass

class Heater:
    error = error
    def __init__(self, config, sensor):
        self.sensor = sensor
        self.name = config.get_name()
        printer = config.get_printer()
        self.min_temp = config.getfloat('min_temp', minval=KELVIN_TO_CELCIUS)
        self.max_temp = config.getfloat('max_temp', above=self.min_temp)
        self.sensor.setup_minmax(self.min_temp, self.max_temp)
        self.sensor.setup_callback(self.temperature_callback)
        self.pwm_delay = self.sensor.get_report_time_delta()
        self.min_extrude_temp = config.getfloat(
            'min_extrude_temp', 170., minval=self.min_temp, maxval=self.max_temp)
        self.max_power = config.getfloat('max_power', 1., above=0., maxval=1.)
        self.lock = threading.Lock()
        self.last_temp = 0.
        self.last_temp_time = 0.
        self.target_temp = 0.
        algos = { 'watermark': ControlBangBang, 'pid': ControlPID,
                  'fopdt': ControlFOPDT }
        algo = config.getchoice('control', algos)
        heater_pin = config.get('heater_pin')
        ppins = printer.lookup_object('pins')
        if algo is ControlBangBang and self.max_power == 1.:
            self.mcu_pwm = ppins.setup_pin('digital_out', heater_pin)
        else:
            self.mcu_pwm = ppins.setup_pin('pwm', heater_pin)
            pwm_cycle_time = config.getfloat(
                'pwm_cycle_time', 0.100, above=0., maxval=self.pwm_delay)
            self.mcu_pwm.setup_cycle_time(pwm_cycle_time)
        self.mcu_pwm.setup_max_duration(MAX_HEAT_TIME)
        is_fileoutput = self.mcu_pwm.get_mcu().is_fileoutput()
        self.can_extrude = self.min_extrude_temp <= 0. or is_fileoutput
        self.control = algo(self, config)
        # pwm caching
        self.next_pwm_time = 0.
        self.last_pwm_value = 0.
        # Load additional modules
        printer.try_load_module(config, "verify_heater %s" % (self.name,))
        printer.try_load_module(config, "pid_calibrate")
        printer.try_load_module(config, "heater_calibrate")
    def set_pwm(self, read_time, value):
        if self.target_temp <= 0.:
            value = 0.
        if ((read_time < self.next_pwm_time or not self.last_pwm_value)
            and abs(value - self.last_pwm_value) < 0.05):
            # No significant change in value - can suppress update
            return 0., self.last_pwm_value
        pwm_time = read_time + self.pwm_delay
        self.next_pwm_time = pwm_time + 0.75 * MAX_HEAT_TIME
        self.last_pwm_value = value
        logging.debug("%s: pwm=%.3f@%.3f (from %.3f@%.3f [%.3f])",
                      self.name, value, pwm_time,
                      self.last_temp, self.last_temp_time, self.target_temp)
        self.mcu_pwm.set_pwm(pwm_time, value)
        return pwm_time, value
    def temperature_callback(self, read_time, temp):
        with self.lock:
            self.last_temp = temp
            self.last_temp_time = read_time
            self.can_extrude = (temp >= self.min_extrude_temp)
            self.control.temperature_callback(read_time, temp)
        #logging.debug("temp: %.3f %f = %f", read_time, temp)
    # External commands
    def set_temp(self, print_time, degrees):
        if degrees and (degrees < self.min_temp or degrees > self.max_temp):
            raise error("Requested temperature (%.1f) out of range (%.1f:%.1f)"
                        % (degrees, self.min_temp, self.max_temp))
        with self.lock:
            self.target_temp = degrees
    def get_temp(self, eventtime):
        print_time = self.mcu_pwm.get_mcu().estimated_print_time(eventtime) - 5.
        with self.lock:
            if self.last_temp_time < print_time:
                return 0., self.target_temp
            return self.last_temp, self.target_temp
    def check_busy(self, eventtime):
        with self.lock:
            return self.control.check_busy(eventtime)
    def set_control(self, control):
        with self.lock:
            old_control = self.control
            self.control = control
            self.target_temp = 0.
        return old_control
    def stats(self, eventtime):
        with self.lock:
            target_temp = self.target_temp
            last_temp = self.last_temp
            last_pwm_value = self.last_pwm_value
        is_active = target_temp or last_temp > 50.
        return is_active, '%s: target=%.0f temp=%.1f pwm=%.3f' % (
            self.name, target_temp, last_temp, last_pwm_value)
    def get_status(self, eventtime):
        with self.lock:
            target_temp = self.target_temp
            last_temp = self.last_temp
        return {'temperature': last_temp, 'target': target_temp}


######################################################################
# Bang-bang control algo
######################################################################

class ControlBangBang:
    def __init__(self, heater, config):
        self.heater = heater
        self.max_delta = config.getfloat('max_delta', 2.0, above=0.)
        self.heating = False
    def temperature_callback(self, read_time, temp):
        if self.heating and temp >= self.heater.target_temp+self.max_delta:
            self.heating = False
        elif not self.heating and temp <= self.heater.target_temp-self.max_delta:
            self.heating = True
        if self.heating:
            self.heater.set_pwm(read_time, self.heater.max_power)
        else:
            self.heater.set_pwm(read_time, 0.)
    def check_busy(self, eventtime):
        return self.heater.last_temp < self.heater.target_temp-self.max_delta


######################################################################
# Proportional Integral Derivative (PID) control algo
######################################################################

PID_SETTLE_DELTA = 1.
PID_SETTLE_SLOPE = .1

class ControlPID:
    def __init__(self, heater, config):
        self.heater = heater
        self.Kp = config.getfloat('pid_Kp') / PID_PARAM_BASE
        self.Ki = config.getfloat('pid_Ki') / PID_PARAM_BASE
        self.Kd = config.getfloat('pid_Kd') / PID_PARAM_BASE
        self.min_deriv_time = config.getfloat('pid_deriv_time', 2., above=0.)
        imax = config.getfloat('pid_integral_max', heater.max_power, minval=0.)
        self.temp_integ_max = imax / self.Ki
        self.prev_temp = AMBIENT_TEMP
        self.prev_temp_time = 0.
        self.prev_temp_deriv = 0.
        self.prev_temp_integ = 0.
    def temperature_callback(self, read_time, temp):
        time_diff = read_time - self.prev_temp_time
        # Calculate change of temperature
        temp_diff = temp - self.prev_temp
        if time_diff >= self.min_deriv_time:
            temp_deriv = temp_diff / time_diff
        else:
            temp_deriv = (self.prev_temp_deriv * (self.min_deriv_time-time_diff)
                          + temp_diff) / self.min_deriv_time
        # Calculate accumulated temperature "error"
        temp_err = self.heater.target_temp - temp
        temp_integ = self.prev_temp_integ + temp_err * time_diff
        temp_integ = max(0., min(self.temp_integ_max, temp_integ))
        # Calculate output
        co = self.Kp*temp_err + self.Ki*temp_integ - self.Kd*temp_deriv
        #logging.debug("pid: %f@%.3f -> diff=%f deriv=%f err=%f integ=%f co=%d",
        #    temp, read_time, temp_diff, temp_deriv, temp_err, temp_integ, co)
        bounded_co = max(0., min(self.heater.max_power, co))
        self.heater.set_pwm(read_time, bounded_co)
        # Store state for next measurement
        self.prev_temp = temp
        self.prev_temp_time = read_time
        self.prev_temp_deriv = temp_deriv
        if co == bounded_co:
            self.prev_temp_integ = temp_integ
    def check_busy(self, eventtime):
        temp_diff = self.heater.target_temp - self.heater.last_temp
        return (abs(temp_diff) > PID_SETTLE_DELTA
                or abs(self.prev_temp_deriv) > PID_SETTLE_SLOPE)


######################################################################
# First Order Plus Delay Time (FOPDT) model
######################################################################

# The key idea of a First Order model is that future temperatures can be
# estimated with the following formula:
#  new_temp = (prev_temp * exp(-time_diff / time_constant)
#              + gain * heater_pwm * (1 - exp(-time_diff / time_constant)))
# Where new_temp and old_temp are relative to the ambient temperature.
# The First Order Plus Delay Time model adds a delay parameter which
# specifies the time delay between changes to the heater_pwm and when
# its effect becomes apparent in measured temperatures. This delay is
# modeled as a smoothing of the first order model temperature over the
# delay time.

INV_AMBIENT_SMOOTH = 1. / 8.
MIN_AMBIENT = 0.
INV_TEMP_SMOOTH = 1. / 2.

class ControlFOPDT:
    def __init__(self, heater, config):
        self.heater = heater
        self.printer = config.get_printer()
        self.name = config.get_name()
        # Model config (gain, time_constant, delay_time)
        self.gain = config.getfloat('gain', above=0.)
        self.inv_gain = 1. / self.gain
        time_constant = config.getfloat('time_constant', above=0.)
        self.inv_time_constant = 1. / time_constant
        delay = config.getfloat('delay_time', above=0.)
        self.inv_delay = 1. / delay
        # Initial temperature check
        self.first_set_temp = True
        self.start_time = self.printer.get_reactor().monotonic()
        # Model calculations
        self.last_pwm_time = self.next_pwm_time = self.last_model_time = 0.
        self.last_pwm = self.next_pwm = 0.
        self.model_temp = self.last_model_temp = self.model_smooth_temp = 0.
        # Ambient calculations
        self.smooth_ambient = AMBIENT_TEMP
        self.did_fault = False
        # Proportional only control
        self.Kp = .7 * time_constant * self.inv_gain
        logging.debug("%s: kp=%.3f", heater.name, self.Kp)
        # check_busy temperature slope detection
        self.prev_temp = AMBIENT_TEMP
        self.prev_temp_time = 0.
        self.temp_slope = 0.
    # Model updating
    def calc_model_temp(self, read_time):
        time_diff = read_time - self.last_pwm_time
        tc_factor = math.exp(-time_diff * self.inv_time_constant)
        return (self.model_temp * tc_factor
                + self.gain * self.last_pwm * (1. - tc_factor))
    def note_temperature(self, read_time, temp):
        # Update internal model (based solely on history of PWM output)
        if self.last_pwm != self.next_pwm and read_time > self.next_pwm_time:
            self.model_temp = self.calc_model_temp(self.next_pwm_time)
            self.last_pwm = self.next_pwm
            self.last_pwm_time = self.next_pwm_time
        self.last_model_temp = model_temp = self.calc_model_temp(read_time)
        time_diff = read_time - self.last_model_time
        self.last_model_time = read_time
        smooth_factor = 1. - math.exp(-time_diff * self.inv_delay)
        self.model_smooth_temp += (
            model_temp - self.model_smooth_temp) * smooth_factor
        # Determine the ambient temperature that would make the model match
        ambient = temp - self.model_smooth_temp
        ambient_factor = 1. - math.exp(-time_diff * INV_AMBIENT_SMOOTH)
        self.smooth_ambient += (ambient - self.smooth_ambient) * ambient_factor
        # Validate calculated ambient is sane
        if (self.smooth_ambient < MIN_AMBIENT and not self.did_fault
            and temp <= self.heater.target_temp):
            logging.error("Heater %s not heating at expected rate"
                          " (model=%.3f ambient=%.3f temp=%.3f)",
                          self.name, self.model_smooth_temp,
                          self.smooth_ambient, temp)
            self.did_fault = True
    def set_pwm(self, read_time, value):
        pwm_time, value = self.heater.set_pwm(read_time, value)
        if self.last_pwm != self.next_pwm:
            self.model_temp = self.calc_model_temp(self.next_pwm_time)
            self.last_pwm = self.next_pwm
            self.last_pwm_time = self.next_pwm_time
        self.next_pwm_time = pwm_time
        self.next_pwm = value
    def note_first_temp(self):
        self.first_set_temp = False
        curtime = self.printer.get_reactor().monotonic()
        if curtime >= self.start_time + 5. / self.inv_time_constant:
            return
        # Assume excess ambient temperature is from previous session
        delta = self.smooth_ambient - AMBIENT_TEMP
        if delta <= 0.:
            return
        self.model_temp += delta
        self.model_smooth_temp += delta
        self.smooth_ambient -= delta
        self.last_pwm_time = self.last_model_time
        logging.info("Adjusting %s model temperature by %.3f", self.name, delta)
    # Control callbacks
    def temperature_callback(self, read_time, temp):
        if self.heater.target_temp and self.first_set_temp:
            self.note_first_temp()
        self.note_temperature(read_time, temp)
        # Calculate temperature slope
        time_diff = read_time - self.prev_temp_time
        temp_diff = temp - self.prev_temp
        self.prev_temp = temp
        smooth_factor = 1. - math.exp(-time_diff * INV_TEMP_SMOOTH)
        self.temp_slope += (temp_diff - self.temp_slope) * smooth_factor
        # Calculate new output
        target_temp = self.heater.target_temp - self.smooth_ambient
        bias = target_temp * self.inv_gain
        temp_err = target_temp - self.last_model_temp
        bounded_co = max(0., min(self.heater.max_power, bias + self.Kp*temp_err))
        self.set_pwm(read_time, bounded_co)
    def check_busy(self, eventtime):
        temp_diff = self.heater.target_temp - self.heater.last_temp
        return (abs(temp_diff) > PID_SETTLE_DELTA
                or abs(self.temp_slope) > PID_SETTLE_SLOPE)


######################################################################
# Sensor and heater lookup
######################################################################

class PrinterHeaters:
    def __init__(self, printer, config):
        self.printer = printer
        self.sensors = {}
        self.heaters = {}
    def add_sensor(self, sensor_type, sensor_factory):
        self.sensors[sensor_type] = sensor_factory
    def setup_heater(self, config):
        heater_name = config.get_name()
        if heater_name == 'extruder':
            heater_name = 'extruder0'
        if heater_name in self.heaters:
            raise config.error("Heater %s already registered" % (heater_name,))
        # Setup sensor
        self.printer.try_load_module(config, "thermistor")
        self.printer.try_load_module(config, "adc_temperature")
        sensor_type = config.get('sensor_type')
        if sensor_type not in self.sensors:
            raise self.printer.config_error("Unknown temperature sensor '%s'" % (
                sensor_type,))
        sensor = self.sensors[sensor_type](config)
        # Create heater
        self.heaters[heater_name] = heater = Heater(config, sensor)
        return heater
    def lookup_heater(self, heater_name):
        if heater_name == 'extruder':
            heater_name = 'extruder0'
        if heater_name not in self.heaters:
            raise self.printer.config_error(
                "Unknown heater '%s'" % (heater_name,))
        return self.heaters[heater_name]

def add_printer_objects(printer, config):
    printer.add_object('heater', PrinterHeaters(printer, config))
