from app.main import clap_sequence_steps
from app.models import Device, DeviceRole, Session


def test_top_camera_never_gets_a_flash_step() -> None:
    main = Device(name="Main", role=DeviceRole.MAIN_CAMERA)
    top = Device(name="Top", role=DeviceRole.TOP_CAMERA)
    secondary = Device(name="Side", role=DeviceRole.SECONDARY_CAMERA)
    session = Session(name="test", devices={
        str(main.device_id): main,
        str(top.device_id): top,
        str(secondary.device_id): secondary,
    })

    steps = clap_sequence_steps(session)

    assert [phase for phase, _ in steps] == ["sync", "camera_id", "main_signature", "main_signature"]
    assert top.device_id not in {device.device_id for _, device in steps if device is not None}
    assert steps[1][1] is secondary
