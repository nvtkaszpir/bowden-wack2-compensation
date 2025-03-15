#!/usr/bin/python3
"""
Slic3r post-processing script to compensate for the wackness of Prusa Mini bowden extrusion

Wack2 extrusion compensation script
This script corrects inconsistent extrusion by E axis based on X axis position/movement.

See https://github.com/prusa3d/Prusa-Firmware-Buddy/issues/2997 for more context



Copyright © 2025 murk-sy [on github.com]
Copyright © 2024 Yedvilun [on github.com] (Slic3r post-processing script to compensate for the bowden curvature)
Copyright © 2023 vgdh [on github.com]

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import argparse
import re
import os
from typing import List


# -----------------------------------------------------
# WACK2 VARIABLES

# Formula: e = 0.6137273 - 0.007144318*x + 0.00002054924*x^2
# based on data measured with dial indicator; curve fitting from mycurvefit.com

# Curve represents negative - leftwise movement
coefficient_a = -0.6137273
coefficient_b = 0.007144318
coefficient_c = -0.00002054924

# Maximum settling of filament in positive - rightwise movement
max_settling_positive = 0.00  # test with up to 0.2

# Leftwise settling in mm of extrusion / 1 mm of X movement
settling_negative = 0.01

# Rightwise settling in mm of extrusion / 1 mm of X movement
settling_positive = 0.01


# If set to true, travel moves will have extruder movement applied as well
# Travel moves are changed to G1 moves with E parameter
use_for_travel_moves = True

# Freeze settling on travel moves
freeze_settle_on_travel = False

# If set to true, default 95% flow gcode will be modified to 100% flow
force_full_flow = False


# Limit maximum extrusion adjustment to a factor of the actual extrusion (excluding travel moves)
# This prevents excessive extrusion adjustment after g2/g3 commands especially
# Note that setting this low will likely cause long term error accumulation
# Hardlimit of 0.8 x means the max extrusion will be: extrusion * (1 + 0.8)
# If no issues are encountered, this can be set to a higher value (like 5)
adjustment_hardlimit_factor = 5

# Removes width related comments to force gcode viewer to use the calculated width
remove_width_comments = True

# Verbose information *before* each extrusion for debugging
verbose = False


def extruder_position_adjustment(x_position):
    return (
        coefficient_a
        + coefficient_b * x_position
        + coefficient_c * x_position * x_position
    )


settling_current = 0.00

# -----------------------------------------------------


class Parameter:
    def __init__(self, name, value):
        self.name = name
        self.value = value

    def __str__(self):
        return f"{self.name}{self.value}"

    def clone(self):
        return Parameter(self.name, self.value)


class State:
    def __init__(
        self,
        x=None,
        y=None,
        z=None,
        e=None,
        f=None,
        move_absolute=True,
        extrude_absolute=True,
        layer_height=0.2,
    ):
        self.X = x
        self.Y = y
        self.Z = z
        self.E = e
        self.F = f
        self.move_is_absolute = move_absolute
        self.extrude_is_absolute = extrude_absolute
        self.layer_height = layer_height

    def clone(self):
        return State(
            self.X,
            self.Y,
            self.Z,
            self.E,
            self.F,
            self.move_is_absolute,
            self.extrude_is_absolute,
            self.layer_height,
        )


class Gcode:
    def __init__(
        self,
        command: str = None,
        parameters: List[Parameter] = None,
        move_is_absolute: bool = True,
        extrude_is_absolute: bool = True,
        comment: str = None,
        previous_state: State = None,
    ):
        self.command = command

        if parameters is None:
            self.parameters = []
        else:
            self.parameters = parameters

        self.move_is_absolute = move_is_absolute
        self.extrude_is_absolute = extrude_is_absolute
        self.comment = comment
        self.previous_state = previous_state
        self.num_line = None

    @staticmethod
    def _format_number(number: int, precision: int) -> str:
        value = round(number, precision)
        value = format(value, "." + str(precision) + "f")
        value = value.rstrip("0").rstrip(".")
        if value.startswith("0."):
            value = value[1:]
        elif value.startswith("-0."):
            value = "-" + value[2:]
        return value

    def __str__(self):
        string = ""
        if self.command is not None:
            string += self.command
            for st in self.parameters:
                if st.value is None:
                    string += f" {st.name}"
                else:
                    if st.name in ('X', 'Y', 'Z'):
                        string += f" {st.name}{Gcode._format_number(st.value, 3)}"
                    elif st.name == "E":
                        string += f" {st.name}{Gcode._format_number(st.value, 5)}"
                        if self.is_xy_movement() is False:
                            comment = ""
                            if self.comment is None:
                                self.comment = comment
                            else:
                                self.comment += f"{comment}"
                    else:
                        string += f" {st.name}{st.value}"

        if self.comment is not None and len(self.comment) > 1:
            if string == "":
                string += f"; {self.comment}"
            else:
                string += f" ; {self.comment}"
        return string

    def clone(self):
        if self.previous_state is None:
            prev_state = State()
        else:
            prev_state = self.previous_state.clone()
        gcode = Gcode(
            self.command,
            move_is_absolute=self.move_is_absolute,
            extrude_is_absolute=self.extrude_is_absolute,
            comment=self.comment,
            previous_state=prev_state,
        )
        for param in self.parameters:
            gcode.parameters.append(param.clone())

        if self.num_line is not None:
            gcode.num_line = self.num_line
        return gcode

    def state(self) -> State:
        if self.previous_state is None:
            _state = State()
            _state.X = 0
            _state.Y = 0
            _state.Z = 0
            _state.E = 0
        else:
            _state = self.previous_state.clone()

        # 2025-03-12 Added rudimentary G2/G3 support and G0 for last state support
        if (
            self.command == "G1"
            or self.command == "G2"
            or self.command == "G3"
            or (self.command == "G0" and use_for_travel_moves is True)
        ):
            for parameter in self.parameters:
                if parameter.name == "X":
                    if _state.move_is_absolute:
                        _state.X = parameter.value
                    else:
                        _state.X += parameter.value
                elif parameter.name == "Y":
                    if _state.move_is_absolute:
                        _state.Y = parameter.value
                    else:
                        _state.Y += parameter.value
                elif parameter.name == "Z":
                    if _state.move_is_absolute:
                        _state.Z = parameter.value
                    else:
                        _state.Z += parameter.value
                elif parameter.name == "E":
                    if _state.extrude_is_absolute:
                        _state.E = parameter.value
                    else:
                        _state.E += parameter.value
                elif parameter.name == "F":
                    _state.F = parameter.value

        _state.move_is_absolute = self.move_is_absolute
        _state.extrude_is_absolute = self.extrude_is_absolute

        return _state

    def is_xy_movement(self):
        # 2025-03-12 Added rudimentary G2/G3 support
        if self.command != "G1" and self.command != "G2" and self.command != "G3":
            return False
        found_x = next(
            (gc for gc in self.parameters if gc.name == "X" and gc.value is not None),
            None,
        )
        found_y = next(
            (gc for gc in self.parameters if gc.name == "Y" and gc.value is not None),
            None,
        )
        if found_x is not None or found_y is not None:
            return True
        return False

    def set_param(self, name, value):
        found = next((gc for gc in self.parameters if gc.name == name), None)
        if found is not None:
            found.value = value
        else:
            self.parameters.append(Parameter(name, value))

    def get_param(self, name):
        found = next((gc for gc in self.parameters if gc.name == name), None)
        if found is not None:
            return found.value


def validate_gcode_command_string(string):
    # The pattern matches a letter followed by a positive number or zero
    pattern = re.compile("^[A-Za-z][0-9]+$")
    # The match method returns None if the string does not match the pattern
    return pattern.match(string) is not None


def parse_gcode_line(gcode_line: str, prev_state: State) -> Gcode:
    gcode = Gcode()
    if prev_state is not None:
        gcode.previous_state = prev_state.clone()
        gcode.extrude_is_absolute = gcode.previous_state.extrude_is_absolute
        gcode.move_is_absolute = gcode.previous_state.move_is_absolute

    gcode_line = gcode_line.strip()

    if not gcode_line:
        return gcode

    if gcode_line.startswith(";") or gcode_line.startswith(
        "\n"
    ):  # If contain only comment
        if gcode_line.endswith("\n"):
            gcode_line = gcode_line[: len(gcode_line) - 1]
        gcode.command = gcode_line.replace(
            "\n",
            "",
        )
        return gcode

    parts = gcode_line.split(";", 1)
    if len(parts) > 1:
        gcode.comment = parts[1].replace("\n", "").replace(";", "").strip()

    gcode_parts = (
        parts[0].strip().split()
    )  # Split the line at semicolon to remove comments

    if (
        validate_gcode_command_string(gcode_parts[0]) is False
    ):  # validate command is one letter and one positive number
        gcode.command = parts[0]
        return gcode

    gcode.command = gcode_parts[0]

    for part in gcode_parts[
        1:
    ]:  # Iterate through the remaining parts and extract key-value pairs
        name = part[0]
        value = part[1:]
        try:
            value = int(value)
        except ValueError:
            try:
                value = float(value)
            except ValueError as e:
                # Just keep everything in name
                name = part
                value = None
        parameter = Parameter(name, value)
        gcode.parameters.append(parameter)

    return gcode


def read_gcode_file(path: str) -> List[Gcode]:
    gcodes = []
    z_height = 0.0
    layer_height = os.getenv("SLIC3R_layer_height")
    last_layer_height = os.getenv("SLIC3R_layer_height")

    print("===== Wack2 compensation script =====")

    print("Reading gcode file")
    with open(path, "r", encoding="utf8") as readfile:
        lines = readfile.readlines()
        print("Gcode file loaded to memory")
        last_state = None
        num_line = 1

        global settling_current  # Wack compensation
        global max_settling_positive
        global settling_positive
        global settling_negative

        parse_progress = 0

        for line in lines:
            gcode = parse_gcode_line(line, last_state)

            if gcode.command == "G90":  # enable absolute coordinates
                gcode.move_is_absolute = True
            elif gcode.command == "G91":  # enable relative coordinates
                gcode.move_is_absolute = False
            elif gcode.command == "M82":  # enable absolute distances for extrusion
                gcode.extrude_is_absolute = True
            elif gcode.command == "M83":  # enable relative distances for extrusion
                gcode.extrude_is_absolute = False
            elif (
                gcode.command == "M221" and force_full_flow is True
            ):  # WACK: force full flow
                # change gcode parameter S95 to S100
                gcode.set_param("S", 100)
                print("Forcing full flow: M221 S95 changed to M221 S100")

            if gcode.command is None:
                continue

            # Skip comments marking width if so selected
            if remove_width_comments and gcode.command.startswith(";WIDTH"):
                continue

            last_state = gcode.state()
            gcode.num_line = num_line
            num_line += 1
            travel_move = False

            # Convert G0 to G1
            if gcode.command == "G0" and use_for_travel_moves is True:
                gcode.command = "G1"
                travel_move = True

            if gcode.command == "G1" or gcode.command == "G2" or gcode.command == "G3":
                x_value = gcode.get_param("X")
                z_value = gcode.get_param("Z")
                e_value = gcode.get_param("E")

                if z_value is not None:
                    # check for Z hop
                    if z_value > z_height:
                        last_layer_height = layer_height
                        layer_height = z_height - z_height
                    else:
                        layer_height = last_layer_height

                if gcode.previous_state is not None and x_value is not None:
                    e_xposition_adjust = extruder_position_adjustment(
                        x_value
                    ) - extruder_position_adjustment(gcode.previous_state.X)

                    x_change = x_value - gcode.previous_state.X

                    if (
                        travel_move is False or freeze_settle_on_travel is False
                    ):  # if NOT travel move OR freeze settle on travel is DISABLED
                        if x_change > 0:
                            # X+ - rightwards movement
                            # settling towards max_settling_positive which is a positive value! therefore, take the smallest of the two
                            settling_current = min(
                                settling_current + settling_positive * x_change,
                                max_settling_positive,
                            )
                        elif x_change < 0:
                            # X- - leftwards movement
                            # settling towards 0! therefore, take the largest of the two
                            settling_current = max(
                                settling_current + settling_negative * x_change, 0
                            )

                        if (
                            settling_current < 0
                            or settling_current > max_settling_positive
                        ):
                            print(
                                "Warning: settling_current out of bounds: "
                                + str(settling_current)
                                + "check the settling_positive and settling_negative values (both must be a positive number)"
                            )

                    # convert to relative extrusion change
                    e_adjust_relative = (
                        (e_xposition_adjust + settling_current) / 180 * x_change
                    )

                    if e_value is not None:
                        # limit extrusion adjustment to a factor of the actual extrusion (excluding travel moves)
                        e_adjust_relative = min(
                            e_adjust_relative, e_value * adjustment_hardlimit_factor
                        )
                        e_adjust_relative = max(
                            e_adjust_relative, -e_value * adjustment_hardlimit_factor
                        )

                        gcode.set_param("E", round(e_value + e_adjust_relative, 5))

                    if e_value is None and use_for_travel_moves is True:
                        gcode.set_param("E", round(e_adjust_relative, 5))
                        gcodes.append(";TRAVEL")

                    if verbose:
                        gcodes.append(
                            ";ABS E adj:"
                            + "{:.5f}".format(e_xposition_adjust)
                            + " settl:"
                            + "{:.5f}".format(settling_current)
                            + " Δx:"
                            + "{:.3f}".format(x_change)
                        )
                        gcodes.append(
                            ";REL E adj:" + "{:.5f}".format(e_adjust_relative)
                        )

            gcodes.append(gcode)
            parse_progress += 1

            if (parse_progress % 100000) == 0:
                parse_progress_percent = int(parse_progress/len(lines)*100)
                print(f"Processed {parse_progress} lines, {parse_progress_percent:d}%")
    readfile.close()
    print("Completed " + str(parse_progress) + " lines, 100%")
    return gcodes


def main():
    parser = argparse.ArgumentParser(
        description="Bowden wack2 compensation post-process"
    )
    parser.add_argument("input_file", metavar="gcode-files", type=str, nargs="+")

    args = parser.parse_args()

    file_path = args.input_file[0]

    gcodes_for_save = read_gcode_file(file_path)

    if os.getenv("SLIC3R_PP_OUTPUT_NAME") is not None:
        destFilePath = os.getenv("SLIC3R_PP_OUTPUT_NAME")

    else:
        destFilePath = file_path

    destFilePath = re.sub(r"\.gcode$", "", destFilePath) + "_wack2.gcode"

    print("Writing to " + destFilePath)

    with open(destFilePath, "w", encoding="utf-8") as writefile:
        for gcode in gcodes_for_save:
            writefile.write(str(gcode) + "\n")
    writefile.close()

    print("Done!")


if __name__ == "__main__":
    main()
