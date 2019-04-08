#!/usr/bin/env python3

import os
from pathlib import Path
import re
import shutil
import subprocess

from matplotlib import pyplot as plt
import numpy as np

import openmc
from openmc.data import ATOMIC_NUMBER, NEUTRON_MASS, K_BOLTZMANN


class Model(object):
    """Monoenergetic, isotropic point source in an infinite geometry.

    Parameters
    ----------
    material : str
        Name of the material.
    density : float
        Density of the material in g/cm^3.
    elements : list of tuple
        List in which each item is a 2-tuple consisting of an element string and
        the atom fraction.
    energy : float
        Energy of the source (eV)
    particles : int
        Number of source particles.
    electron_treatment : {'led' or 'ttb'}
        Whether to deposit electron energy locally ('led') or create secondary
        bremsstrahlung photons ('ttb').
    code : {'mcnp', 'serpent'}
        Code to validate against
    suffix : str
        Photon cross section suffix
    library : str
        XSDIR directory file. If specified, it will be used to locate the ACE
        table corresponding to the given element and suffix, and an HDF5
        library that can be used by OpenMC will be created from the data.
    serpent_pdata : str
        Directory containing the additional data files needed for photon
        physics in Serpent.
    name : str
        Name used for output.

    Attributes
    ----------
    material : str
        Name of the material.
    density : float
        Density of the material in g/cm^3.
    elements : list of tuple
        List in which each item is a 2-tuple consisting of an element string and
        the atom fraction.
    energy : float
        Energy of the source (eV)
    particles : int
        Number of source particles.
    electron_treatment : {'led' or 'ttb'}
        Whether to deposit electron energy locally ('led') or create secondary
        bremsstrahlung photons ('ttb').
    code : {'mcnp', 'serpent'}
        Code to validate against
    suffix : str
        Photon cross section suffix
    library : str
        XSDIR directory file. If specified, it will be used to locate the ACE
        table corresponding to the given element and suffix, and an HDF5
        library that can be used by OpenMC will be created from the data.
    serpent_pdata : str
        Directory containing the additional data files needed for photon
        physics in Serpent.
    name : str
        Name used for output.
    bins : int
        Number of bins in the energy grid
    batches : int
        Number of batches to simulate
    cutoff_energy: float
        Photon cutoff energy (eV)

    """

    def __init__(self, material, density, elements, energy, particles,
                 electron_treatment, code, suffix, library=None,
                 serpent_pdata=None, name=None):
        self._bins = 500
        self._batches = 100
        self._cutoff_energy = 1.e3

        self.material = material
        self.density = density
        self.elements = elements
        self.energy = energy
        self.particles = particles
        self.electron_treatment = electron_treatment
        self.code = code
        self.suffix = suffix
        self.library = library
        self.serpent_pdata = serpent_pdata
        self.name = name

    @property
    def energy(self):
        return self._energy

    @property
    def particles(self):
        return self._particles

    @property
    def code(self):
        return self._code

    @property
    def suffix(self):
        return self._suffix

    @property
    def library(self):
        return self._library

    @property
    def serpent_pdata(self):
        return self._serpent_pdata

    @energy.setter
    def energy(self, energy):
        if energy <= self._cutoff_energy:
            msg = (f'Energy {energy} eV is not above the cutoff energy '
                   f'{self._cutoff_energy} eV.')
            raise ValueError(msg)
        self._energy = energy

    @particles.setter
    def particles(self, particles):
        if particles % self._batches != 0:
            msg = (f'Number of particles {particles} must be divisible by '
                   f'the number of batches {self._batches}.')
            raise ValueError(msg)
        self._particles = particles

    @code.setter
    def code(self, code):
        if code not in ['mcnp', 'serpent']:
            msg = (f'Unsupported code {code}: code must be either "mcnp" or '
                   f'"serpent".')
            raise ValueError(msg)
        executable = 'mcnp6' if code == 'mcnp' else 'sss2'
        if not shutil.which(executable, os.X_OK):
            msg = f'Unable to locate executable {executable} in path.'
            raise ValueError(msg)
        self._code = code

    @suffix.setter
    def suffix(self, suffix):
        if not re.match('0[1-4]p|12p', suffix):
            msg = f'Unsupported cross section suffix {suffix}.'
            raise ValueError(msg)
        self._suffix = suffix

    @library.setter
    def library(self, library):
        if library is not None:
            library = Path(library)
            if not library.is_file():
                msg = f'XSDIR {library} is not a file.'
                raise ValueError(msg)
        self._library = library

    @serpent_pdata.setter
    def serpent_pdata(self, serpent_pdata):
        if self.code == 'serpent':
            if serpent_pdata is None:
                msg = ('Serpent photon data path is required to run a '
                       'calculation with Serpent.')
                raise ValueError(msg)
            serpent_pdata = Path(serpent_pdata).resolve()
            if not serpent_pdata.is_dir():
                msg = (f'Serpent photon data path {serpent_pdata} is not a '
                       f'directory.')
                raise ValueError(msg)
        self._serpent_pdata = serpent_pdata

    def _create_library(self):
        """Convert the ACE data from the MCNP or Serpent distribution into an
        HDF5 library that can be used by OpenMC.

        """
        # Create data library and directory for HDF5 files
        data_lib = openmc.data.DataLibrary()
        os.makedirs('openmc', exist_ok=True)

        # Get names of the ACE tables for all nuclides in model
        datapath = None
        entries = {}
        for element, fraction in self.elements:
            # Name of photon cross section table
            Z = ATOMIC_NUMBER[element]
            name = f'{1000*Z}.{self.suffix}'
            entries[name] = None

            # TODO: Currently the neutron libraries are still read in to
            # OpenMC even when doing pure photon transport, so we need to
            # locate them and register them with the library.
            path = os.getenv('OPENMC_CROSS_SECTIONS')
            lib = openmc.data.DataLibrary.from_xml(path)
            element = openmc.Element(element)
            for nuclide, _, _ in element.expand(fraction, 'ao'):
                h5_file = lib.get_by_material(nuclide)['path']
                data_lib.register_file(h5_file)

        # Get the location of the tables from the XSDIR directory file
        with open(self.library) as f:
            # Read the datapath if it is specified
            line = f.readline()
            tokens = re.split('\s|=', line)
            if tokens[0].lower() == 'datapath':
                datapath = Path(tokens[1])

            line = f.readline()
            while line:
                # Handle continuation lines
                while line[-2] == '+':
                    line += f.readline()
                    line = line.replace('+\n', '')

                tokens = line.split()

                # Store the entry if we need this table
                if tokens[0] in entries.keys():
                    entries[tokens[0]] = tokens

                # Check if we found all the entries
                if None not in entries.values():
                    break

                line = f.readline()

        lines = []
        for name, entry in entries.items():
            if entry is None:
                msg = f'Could not locate table {name} in XSDIR {self.library}.'
                raise ValueError(msg)

            # Get the access route if it is specified; otherwise, set the parent
            # directory of XSDIR as the datapath
            if datapath is None:
                if entry[3] != '0':
                    datapath = Path(entry[3])
                else:
                    datapath = self.library.parent

            # Get the full path to the ace library
            path = datapath / entry[2]
            if not path.is_file():
                msg = f'ACE file {path} does not exist.'
                raise ValueError(msg)

            # Get the data needed for the Serpent XSDATA directory file.
            if self.code == 'serpent':
                atomic_weight = float(entry[1]) * NEUTRON_MASS
                temperature = float(entry[9]) / K_BOLTZMANN * 1e6
                ZA, _ = name.split('.')
                lines.append(f'{name} {name} 5 {ZA} 0 {atomic_weight} '
                             f'{temperature} 0 {path}')

            # Get the ACE table
            print(f'Converting table {name} from library {path}...')
            table = openmc.data.ace.get_table(path, name)

            # Convert cross section data
            data = openmc.data.IncidentPhoton.from_ace(table)

            # Export HDF5 files and register with library
            h5_file = Path('openmc') / f'{data.name}.h5'
            data.export_to_hdf5(h5_file, 'w')
            data_lib.register_file(h5_file)

        # Write cross_sections.xml
        data_lib.export_to_xml(Path('openmc') / 'cross_sections.xml')

        # Write the Serpent XSDATA file
        if self.code == 'serpent':
            os.makedirs('serpent', exist_ok=True)
            with open(Path('serpent') / 'xsdata', 'w') as f:
                f.write('\n'.join(lines))

    def _make_openmc_input(self):
        """Generate the OpenMC input XML

        """
        # Directory from which openmc is run
        os.makedirs('openmc', exist_ok=True)
        
        # Define material
        mat = openmc.Material()
        for element, fraction in self.elements:
            mat.add_element(element, fraction)
        mat.set_density('g/cm3', self.density)
        materials = openmc.Materials([mat])
        if self.library is not None:
            xs_path = (Path('openmc') / 'cross_sections.xml').resolve()
            materials.cross_sections = str(xs_path)
        materials.export_to_xml(Path('openmc') / 'materials.xml')

        # Set up geometry
        sphere = openmc.Sphere(boundary_type='vacuum', r=1.e9)
        cell = openmc.Cell(fill=materials, region=-sphere)
        geometry = openmc.Geometry([cell])
        geometry.export_to_xml(Path('openmc') / 'geometry.xml')

        # Define source
        source = openmc.Source()
        source.space = openmc.stats.Point((0,0,0))
        source.angle = openmc.stats.Isotropic()
        source.energy = openmc.stats.Discrete([self.energy], [1.])
        source.particle = 'photon'

        # Settings
        settings = openmc.Settings()
        settings.source = source
        settings.particles = self.particles // self._batches
        settings.run_mode = 'fixed source'
        settings.batches = self._batches
        settings.photon_transport = True
        settings.electron_treatment = self.electron_treatment
        settings.cutoff = {'energy_photon' : self._cutoff_energy}
        settings.export_to_xml(Path('openmc') / 'settings.xml')
 
        # Define tallies
        energy_bins = np.logspace(np.log10(self._cutoff_energy),
                                  np.log10(1.0001*self.energy), self._bins+1)
        energy_filter = openmc.EnergyFilter(energy_bins)
        particle_filter = openmc.ParticleFilter('photon')
        tally = openmc.Tally(name='photon flux')
        tally.filters = [energy_filter, particle_filter]
        tally.scores = ['flux']
        tallies = openmc.Tallies([tally])
        tallies.export_to_xml(Path('openmc') / 'tallies.xml')

    def _make_mcnp_input(self):
        """Generate the MCNP input file

        """
        # Directory from which MCNP will be run
        os.makedirs('mcnp', exist_ok=True)

        # Create the problem description
        lines = ['Point source in infinite geometry']
 
        # Create the cell cards: material 1 inside sphere, void outside
        lines.append('c --- Cell cards ---')
        lines.append(f'1 1 -{self.density} -1 imp:p=1')
        lines.append('2 0 1 imp:p=0')
 
        # Create the surface cards: sphere centered on origin with 1e9 cm
        # radius and  reflective boundary conditions
        lines.append('')
        lines.append('c --- Surface cards ---')
        lines.append('*1 so 1.0e9')
 
        # Create the data cards
        lines.append('')
        lines.append('c --- Data cards ---')
 
        # Materials
        material_card = 'm1'
        for element, fraction in self.elements:
            Z = openmc.data.ATOMIC_NUMBER[element]
            material_card += f' {Z}000.12p -{fraction}'
        lines.append(material_card)

        # Energy in MeV
        energy = self.energy * 1e-6
        cutoff_energy = self._cutoff_energy * 1e-6

        # Physics: photon transport, 1 keV photon cutoff energy
        if self.electron_treatment == 'led':
            flag = 1
        else:
            flag = 'j'
        lines.append('mode p')
        lines.append(f'phys:p j {flag} j j j')
        lines.append(f'cut:p j {cutoff_energy}')
 
        # Source definition: isotropic point source at center of sphere
        lines.append(f'sdef cel=1 erg={energy}')
 
        # Tallies: photon flux over cell
        lines.append('f4:p 1')
        lines.append(f'e4 {cutoff_energy} {self._bins-1}ilog {1.0001*energy}')
 
        # Problem termination: number of particles to transport
        lines.append(f'nps {self.particles}')
 
        # Write the problem
        with open(Path('mcnp') / 'inp', 'w') as f:
            f.write('\n'.join(lines))

    def _make_serpent_input(self):
        """Generate the Serpent input file

        """
        # Directory from which Serpent will be run
        os.makedirs('serpent', exist_ok=True)

        # Create the problem description
        lines = ['% Point source in infinite geometry']
        lines.append('')

        # Set the cross section library directory
        if self.library is not None:
            xsdata = (Path('serpent') / 'xsdata').resolve()
            lines.append(f'set acelib "{xsdata}"')

        # Set the photon data directory
        lines.append(f'set pdatadir "{self.serpent_pdata}"')
        lines.append('')

        # Create the cell cards: material 1 inside sphere, void outside
        lines.append('% --- Cell cards ---')
        lines.append('cell 1 0 m1 -1')
        lines.append('cell 2 0 outside 1')
        lines.append('')

        # Create the surface cards: sphere centered on origin with 1e9 cm
        # radius and vacuum boundary conditions
        lines.append('% --- Surface cards ---')
        lines.append('surf 1 sph 0.0 0.0 0.0 1.e9')
        lines.append('')

        # Create the material cards
        lines.append('% --- Material cards ---')
        lines.append(f'mat m1 -{self.density}')

        # Add element data
        for element, fraction in self.elements:
            Z = ATOMIC_NUMBER[element]
            name = f'{1000*Z}.{self.suffix}'
            lines.append(f'{name} {fraction}')

        # Turn on unresolved resonance probability treatment
        lines.append('set ures 1')

        # Set electron treatment
        if self.electron_treatment == 'led':
            lines.append('set ttb 0')
        else:
            lines.append('set ttb 1')

        # Turn on Doppler broadening of Compton scattered photons (on by
        # default)
        lines.append('set cdop 1')

        # Energy in MeV
        energy = self.energy * 1e-6
        cutoff_energy = self._cutoff_energy * 1e-6

        # Set cutoff energy
        lines.append(f'set ecut 0 {cutoff_energy}')
        lines.append('')

        # External source mode with isotropic point source at center of sphere
        lines.append('% --- Set external source mode ---')
        lines.append(f'set nps {self.particles} {self._batches}')
        lines.append(f'src 1 g se {energy} sp 0.0 0.0 0.0')
        lines.append('')

        # Detector definition: flux energy spectrum
        lines.append('% --- Detector definition ---')
        lines.append('det 1 de 1 dc 1')

        # Energy grid definition: equal lethargy spacing
        lines.append(f'ene 1 3 {self._bins} {cutoff_energy} {1.0001*energy}')
        lines.append('')

        # Write the problem
        with open(Path('serpent') / 'input', 'w') as f:
            f.write('\n'.join(lines))

    def _read_openmc_results(self):
        """Extract the results from the OpenMC statepoint

        """
        # Read the results from the OpenMC statepoint
        path = Path('openmc') / f'statepoint.{self._batches}.h5'
        with openmc.StatePoint(path) as sp:
            t = sp.get_tally(name='photon flux')
            x = t.find_filter(openmc.EnergyFilter).bins[:,1] * 1e-6
            y = t.mean[:,0,0]
            sd = t.std_dev[:,0,0]

        # Normalize the spectrum
        cutoff_energy = self._cutoff_energy * 1e-6
        y /= np.diff(np.insert(x, 0, cutoff_energy))*sum(y)

        return x, y, sd

    def _read_mcnp_results(self):
        """Extract the results from the MCNP output file

        """
        with open(Path('mcnp') / 'outp', 'r') as f:
            text = f.read()
            p = text.find('1tally')
            p = text.find('energy', p) + 10
            q = text.find('total', p)
            t = np.fromiter(text[p:q].split(), float)
            t.shape = (len(t) // 3, 3)
            x = t[1:,0]
            y = t[1:,1]
            sd = t[1:,2]
 
        # Normalize the spectrum
        cutoff_energy = self._cutoff_energy * 1e-6
        y /= np.diff(np.insert(x, 0, cutoff_energy))*sum(y)

        return x, y, sd

    def _read_serpent_results(self):
        """Extract the results from the Serpent output file

        """
        with open(Path('serpent') / 'input_det0.m', 'r') as f:
            text = f.read().split()
            n = self._bins
            t = np.fromiter(text[3:3+12*n], float).reshape(n, 12)
            e = np.fromiter(text[7+12*n:7+15*n], float).reshape(n, 3)
            x = e[:,1]
            y = t[:,10]
            sd = t[:,11]

        # Normalize the spectrum
        cutoff_energy = self._cutoff_energy * 1e-6
        y /= np.diff(np.insert(x, 0, cutoff_energy))*sum(y)

        return x, y, sd

    def _plot(self):
        """Extract and plot the results
 
        """
        # Read results
        x1, y1, _ = self._read_openmc_results()
        if self.code == 'serpent':
            x2, y2, sd = self._read_serpent_results()
        else:
            x2, y2, sd = self._read_mcnp_results()

        # Compute the relative error
        err = np.zeros_like(y2)
        idx = np.where(y2 > 0)
        err[idx] = (y1[idx] - y2[idx])/y2[idx]
 
        # Set up the figure
        fig = plt.figure(1, facecolor='w', figsize=(8,8))
        ax1 = fig.add_subplot(111)
 
        # Create a second y-axis that shares the same x-axis, keeping the first
        # axis in front
        ax2 = ax1.twinx()
        ax1.set_zorder(ax2.get_zorder() + 1)
        ax1.patch.set_visible(False)
 
        # Plot the spectra
        label = 'Serpent' if self.code == 'serpent' else 'MCNP'
        ax1.loglog(x2, y2, 'r', linewidth=1, label=label)
        ax1.loglog(x1, y1, 'b', linewidth=1, label='OpenMC', linestyle='--')
 
        # Plot the relative error and uncertainties
        ax2.semilogx(x2, err, color=(0.2, 0.8, 0.0), linewidth=1)
        ax2.semilogx(x2, 2*sd, color='k', linestyle='--', linewidth=1)
        ax2.semilogx(x2, -2*sd, color='k', linestyle='--', linewidth=1)
 
        # Set grid and tick marks
        ax1.tick_params(axis='both', which='both', direction='in', length=10)
        ax1.grid(b=False, axis='both', which='both')
        ax2.tick_params(axis='y', which='both', right=False)
        ax2.grid(b=True, which='both', axis='both', alpha=0.5, linestyle='--')
 
        # Energy in MeV
        energy = self.energy * 1e-6
        cutoff_energy = self._cutoff_energy * 1e-6

        # Set axes labels and limits
        ax1.set_xlim([cutoff_energy, energy])
        ax1.set_xlabel('Energy (MeV)', size=12)
        ax1.set_ylabel('Spectrum', size=12)
        ax1.legend()
        ax2.set_ylabel("Relative error", size=12)
        title = f'{self.material}, {energy:.1e} MeV Source'
        plt.title(title)
 
        # Save plot
        os.makedirs('plots', exist_ok=True)
        if self.name is not None:
            name = self.name
        else:
            name = f'{self.material}-{energy:.1e}MeV'
        plt.savefig(Path('plots') / f'{name}.png', bbox_inches='tight')
        plt.close()

    def run(self):
        """Generate inputs, run problem, and plot results.
 
        """
        if self.library is not None:
            self._create_library()

        # Generate input files
        if self.code == 'serpent':
            self._make_serpent_input()
            args = ['sss2', 'input']
        else:
            self._make_mcnp_input()
            args = ['mcnp6']
            if self.library is not None:
                args.append(f'XSDIR={self.library}')

        # Remove old MCNP output files
        for f in ('outp', 'runtpe'):
            try:
                os.remove(Path('mcnp') / f)
            except OSError:
                pass
 
        self._make_openmc_input()

        # Run code and capture and print output
        p = subprocess.Popen(args, cwd=self.code, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, universal_newlines=True)
        while True:
            line = p.stdout.readline()
            if not line and p.poll() is not None:
                break
            print(line, end='')

        openmc.run(cwd='openmc')

        self._plot()
