# Automatic heater calibration command
#
# Copyright (C) 2016-2018  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math, logging
import heater, mathutil

class HeaterCalibrate:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command(
            'HEATER_CALIBRATE', self.cmd_HEATER_CALIBRATE,
            desc=self.cmd_HEATER_CALIBRATE_help)
    cmd_HEATER_CALIBRATE_help = "Run heater calibration test"
    def cmd_HEATER_CALIBRATE(self, params):
        heater_name = self.gcode.get_str('HEATER', params)
        target = self.gcode.get_float('TARGET', params)
        write_file = self.gcode.get_int('WRITE_FILE', params, 0)
        pheater = self.printer.lookup_object('heater')
        try:
            heater = pheater.lookup_heater(heater_name)
        except self.printer.config_error as e:
            raise self.gcode.error(str(e))
        print_time = self.printer.lookup_object('toolhead').get_last_move_time()
        calibrate = ControlBumpTest(self.printer, heater)
        old_control = heater.set_control(calibrate)
        try:
            heater.set_temp(print_time, target)
        except heater.error as e:
            heater.set_control(old_control)
            raise self.gcode.error(str(e))
        self.gcode.bg_temp(heater)
        heater.set_control(old_control)
        if write_file:
            calibrate.write_file('/tmp/heattest.txt')
        gain, time_constant, delay = calibrate.calc_fopdt()
        self.gcode.respond_info(
            "Heater parameters: gain=%.3f time_constant=%.3f delay_time=%.3f\n"
            "To use these parameters, update the printer config file with\n"
            "the above and then issue a RESTART command" % (
                gain, time_constant, delay))

class ControlBumpTest:
    def __init__(self, printer, heater):
        self.reactor = printer.get_reactor()
        self.heater = heater
        # State tracking
        self.state = 0
        self.done_temperature = 0.
        # Samples
        self.last_pwm = 0.
        self.pwm_samples = []
        self.temp_samples = []
        # Calculations
        self.ambient_temp = 0.
    # Heater control
    def set_pwm(self, read_time, value):
        if value != self.last_pwm:
            self.pwm_samples.append((read_time + self.heater.pwm_delay, value))
            self.last_pwm = value
        self.heater.set_pwm(read_time, value)
    def temperature_callback(self, read_time, temp):
        self.temp_samples.append((read_time, temp))
        if self.state == 0:
            self.set_pwm(read_time, 0.)
            if len(self.temp_samples) >= 20:
                # XXX - verify ambient temperature is valid
                self.state += 1
        elif self.state == 1:
            if temp < self.heater.target_temp:
                self.set_pwm(read_time, self.heater.max_power)
                return
            self.set_pwm(read_time, 0.)
            start_temp = self.temp_samples[0][1]
            self.done_temperature = (
                start_temp + (self.heater.target_temp - start_temp) * .35)
            self.heater.target_temp = self.done_temperature # XXX
            self.state += 1
        elif self.state == 2:
            self.set_pwm(read_time, 0.)
            if temp <= self.done_temperature:
                self.state += 1
    def check_busy(self, eventtime):
        if self.state < 3:
            return True
        return False
    # First Order Plus Delay Time calculation
    def model_smoothed_fopdt(self, gain, time_constant, delay):
        heater_on_time = self.pwm_samples[0][0]
        heater_off_time = self.pwm_samples[1][0]
        gain *= self.pwm_samples[0][1]
        ambient_temp = self.ambient_temp
        inv_time_constant = 1. / time_constant
        inv_delay = 1. / delay
        heat_time = heater_off_time - heater_on_time
        peak_temp = gain * (1. - math.exp(-heat_time * inv_time_constant))
        smooth_temp = last_time = 0.
        out = []
        for time, measured_temp in self.temp_samples:
            rel_temp = 0.
            if time > heater_off_time:
                cool_time = time - heater_off_time
                rel_temp = peak_temp * math.exp(-cool_time * inv_time_constant)
            elif time > heater_on_time:
                heat_time = time - heater_on_time
                rel_temp = gain * (1. - math.exp(-heat_time * inv_time_constant))
            time_diff = time - last_time
            last_time = time
            smooth_factor = 1. - math.exp(-time_diff * inv_delay)
            smooth_temp += (rel_temp - smooth_temp) * smooth_factor
            out.append(ambient_temp + smooth_temp)
        return out
    def least_squares_error(self, params):
        gain = params['gain']
        time_constant = params['time_constant']
        delay = params['delay']
        if gain <= 0. or time_constant <= 0. or delay <= 0.:
            return 9.9e99
        model = self.model_smoothed_fopdt(gain, time_constant, delay)
        err = 0.
        for (time, measured_temp), model_temp in zip(self.temp_samples, model):
            err += (measured_temp-model_temp)**2
        self.reactor.pause(self.reactor.NOW) # XXX
        return err
    def calc_fopdt(self):
        # Determine the ambient temperature
        heater_on_time, max_power = self.pwm_samples[0]
        pre_heat = [temp for time, temp in self.temp_samples
                    if time <= heater_on_time]
        self.ambient_temp = sum(pre_heat) / len(pre_heat)
        # Initial fopdt guesses
        params = {}
        maxtemp, maxtemptime = max([(temp, time)
                                    for time, temp in self.temp_samples])
        params['gain'] = maxtemp * 2.
        params['time_constant'] = self.temp_samples[-1][0] - maxtemptime
        params['delay'] = 10.
        # Fit smoothed fopdt model to measured temperatures
        new_params = mathutil.coordinate_descent(
            ('gain', 'time_constant', 'delay'), params, self.least_squares_error)
        gain = new_params['gain']
        time_constant = new_params['time_constant']
        delay = new_params['delay']
        logging.info("calc_fopdt: ambient_temp=%.3f gain=%.3f"
                     " time_constant=%.3f delay_time=%.3f",
                     self.ambient_temp, gain, time_constant, delay)
        return gain, time_constant, delay
    # Offline analysis helper
    def write_file(self, filename):
        pwm = ["pwm: %.3f %.3f" % (time, value)
               for time, value in self.pwm_samples]
        out = ["%.3f %.3f" % (time, temp) for time, temp in self.temp_samples]
        f = open(filename, "wb")
        f.write('\n'.join(pwm + out))
        f.close()

def load_config(config):
    return HeaterCalibrate(config)
