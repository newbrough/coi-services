#from interface.services.icontainer_agent import ContainerAgentClient
from interface.objects import AttachmentType
#from pyon.ion.endpoint import ProcessRPCClient
from pyon.public import Container, IonObject
from pyon.util.log import log
from pyon.util.containers import DotDict
from pyon.util.int_test import IonIntegrationTestCase
from pyon.event.event import EventSubscriber

from gevent.event import AsyncResult

from interface.services.sa.idata_product_management_service import DataProductManagementServiceClient
from interface.services.sa.idata_acquisition_management_service import DataAcquisitionManagementServiceClient
from interface.services.sa.iinstrument_management_service import InstrumentManagementServiceClient
from interface.services.sa.iobservatory_management_service import ObservatoryManagementServiceClient
from interface.services.dm.ipubsub_management_service import PubsubManagementServiceClient
from interface.services.coi.iresource_registry_service import ResourceRegistryServiceClient

from pyon.core.exception import BadRequest, NotFound, Conflict, Inconsistent
from pyon.public import RT, LCS, PRED
from nose.plugins.attrib import attr
import unittest

from ion.services.sa.test.helpers import any_old
from ion.services.sa.observatory.instrument_site_impl import InstrumentSiteImpl
from ion.services.sa.observatory.platform_site_impl import PlatformSiteImpl
from ion.services.sa.instrument.platform_agent_impl import PlatformAgentImpl
from ion.services.sa.instrument.instrument_device_impl import InstrumentDeviceImpl
from ion.services.sa.instrument.sensor_device_impl import SensorDeviceImpl



@attr('INT', group='sa')
class TestL4CiSaReqs(IonIntegrationTestCase):
    """
    assembly integration tests at the service level
    """

    def setUp(self):
        # Start container
        self._start_container()
        self.container.start_rel_from_url('res/deploy/r2deploy.yml')

        # Now create client to DataProductManagementService
        self.client = DotDict()
        self.client.DAMS = DataAcquisitionManagementServiceClient(node=self.container.node)
        self.client.DPMS = DataProductManagementServiceClient(node=self.container.node)
        self.client.IMS  = InstrumentManagementServiceClient(node=self.container.node)
        self.client.OMS = ObservatoryManagementServiceClient(node=self.container.node)
        self.client.PSMS = PubsubManagementServiceClient(node=self.container.node)

        self.client.RR   = ResourceRegistryServiceClient(node=self.container.node)
        self.RR = self.client.RR 

    @unittest.skip('this test just for debugging setup')
    def test_just_the_setup(self):
        return


    def test_l4_ci_sa_rq_161_235_336(self):
        instrument_device_id, _ =          self.RR.create(any_old(RT.InstrumentDevice))
        #attachments to instrument
        self.RR.create_attachment(instrument_device_id, IonObject(RT.Attachment,
                                                                 name="blah",
                                                                 description="blah blah",
                                                                 content="blah blah blah ",
                                                                 content_type="text/plain",
                                                                 keywords=["blah"],
                                                                 attachment_type=AttachmentType.BLOB))


        attachments, _ = self.RR.find_objects(instrument_device_id, PRED.hasAttachment, RT.Attachment, True)
        self.assertEqual(len(attachments), 1)
        a = self.RR.read_attachment(attachments[0])

        self.assertEqual("blah", a.name)
        self.assertEqual("text/plain", a.content_type)
        self.assertIn("blah", a.keywords)
        self.assertEqual(a.content, "blah " * 3)

        log.info("L4-CI-SA-RQ-161")
        log.info("L4-CI-SA-RQ-235")
        log.info("L4-CI-SA-RQ-336")


    def test_l4_ci_sa_rq_145_323(self):
        """
        Instrument management shall update physical resource metadata when change occurs

        For example, when there is a change of state.

        note from maurice 2012-05-18: consider this to mean a change of stored RR data
        """

        inst_obj = any_old(RT.InstrumentDevice)
        instrument_device_id, _ = self.RR.create(inst_obj)
        self.received_event = AsyncResult()

        #Create subscribers for agent and driver events.

        def consume_event(*args, **kwargs):
            self.received_event.set(True)
            log.info("L4-CI-SA-RQ-323")
            log.info("L4-CI-SA-RQ-145")

        event_sub = EventSubscriber(event_type="ResourceModifiedEvent", callback=consume_event)
        event_sub.activate()


        inst_obj = self.RR.read(instrument_device_id)
        inst_obj.description = "brand new description"
        self.RR.update(inst_obj)

        #wait for event
        result = self.received_event.get(timeout=10)
        event_sub.deactivate()

        self.assertTrue(result)



    @unittest.skip('Requires platform agent, plus interaction between it and instrument agent')
    def test_l4_ci_sa_rq_315(self):
        """
        Instrument management shall implement physical resource command queueing

        For use on intermittently-connected platforms.
        The batch file is executed the next time the platform is connected.
        """
        pass



    @unittest.skip('to be completed')
    def test_l4_ci_sa_rq_314_320_321_225_335(self):
        """
        314: Instrument management control capabilities shall submit requests to control a physical resource as required by policy        
        320:Instrument management shall implement the acquire life cycle activity
        321:Instrument management shall implement the release life cycle activity

        225: The instrument activation services shall support the deactivation of physical resources.
        225: Instrument activation shall support transition to the deactivated state of instruments
        225: Deactivation means the instrument is no longer available for use. This takes the instrument off-line, powers it down and shuts down its Instrument Agent.
        The predecessor to the actual submission of a command.  Then release instrument

        335: Instrument activation shall support transition to the retired state of instruments


        note from maurice 2012-05-18: coord with stephen
        """
        pass



    ###### not done yet, waiting to hear back from Tim m.


    @unittest.skip('to be completed')
    def test_l4_ci_sa_rq_138(self):
        """
        Physical resource control shall be subject to policy

        Instrument management control capabilities shall be subject to policy

        The actor accessing the control capabilities must be authorized to send commands.

        note from maurice 2012-05-18: Talk to tim M to verify that this is policy.  If it is then talk with Stephen to get an example of a policy test and use that to create a test stub that will be completed when we have instrument policies.
        """
        pass


    @unittest.skip('to be completed')
    def test_l4_ci_sa_rq_139(self):
        """
        The control services shall support resource control by another resource

        Instrument management shall implement resource control by another resource

        Subject to policy.

        note from maurice 2012-05-18: Talk to tim M to verify that this is policy.  If it is then talk with Stephen to get an example of a policy test and use that to create a test stub that will be completed when we have instrument policies.
        """
        pass

    @unittest.skip('to be completed')
    def test_l4_ci_sa_rq_380(self):
        """
        Instrument management shall implement the synoptic notion of time

        Software to utilize NTP or PTP synoptic time for instrument and platform management purposes

        note from maurice 2012-05-18: will need to talk to tim m / alan about this, until then just write a test shell with outline of how this can be generated
        """
        pass

    @unittest.skip('to be completed')
    def test_l4_ci_sa_rq_226(self):
        """
        Test services to apply test procedures to physical resources shall be provided.

        Instrument management shall implement qualification of physical resources on ION

        This includes testing of the instrument and its Instrument Agent for compatibility with ION. This could be carried out using the Instrument Test Kit, or online for a newly installed instrument on the marine infrastructure. The details of the qualification tests are instrument specific at the discretion of the marine operator.

        note from maurice 2012-05-18: Bill may already have this or perhaps this is just an inspection test.  Talk with Tim M.

        """
        pass


